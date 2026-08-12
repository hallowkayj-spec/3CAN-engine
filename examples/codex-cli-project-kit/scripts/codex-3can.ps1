[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateScript({
        $unsupportedPackagedActions = @('deploy', 'maintenance', 'autoloop')
        if ($unsupportedPackagedActions -contains $_) {
            throw "Action '$_' is unsupported in this release package because its implementation harness is not shipped."
        }
        $supportedPackagedActions = @(
            'doctor', 'bootstrap', 'start', 'route', 'supervise',
            'supervise-status', 'prepare', 'fail', 'failure-gate-sync',
            'done', 'compact', 'state', 'clear', 'pr-check', 'pr-create'
        )
        if ($supportedPackagedActions -notcontains $_) {
            throw "Unsupported 3CAN action '$($_)'."
        }
        return $true
    })]
    [string]$Action,

    [string]$AgentId,
    [string]$Role = 'frontend',
    [string]$Task = 'codex session',
    [string]$TaskDescription,
    [string[]]$TargetFiles = @(),
    [string[]]$ScopeKeywords = @(),
    [string]$ToolName = 'apply_patch',
    [string]$ToolInputSummary,
    [string]$CommandSummary,
    [string]$ErrorExcerpt,
    [string]$Diagnosis,
    [string]$OperationClass,
    [string]$Component,
    [string]$ErrorType,
    [string]$Detail,
    [string[]]$AffectedNodes = @(),
    [string[]]$ResolvedErrors = @(),
    [string[]]$ErrorDispositions = @(),
    [string]$SolutionSummary,
    [string]$RootCause,
    [string[]]$VerificationEvidence = @(),
    [string]$FixedIn,
    [string]$TaskSummary,
    [string]$Title,
    [string]$Body,
    [string]$BodyFile,
    [string]$HeadBranch,
    [string]$BaseBranch = 'main',
    [string]$ApprovalId,
    [string]$GitWorktree,
    [string[]]$NextSteps = @(),
    [string[]]$RelatedNodes = @(),
    [string[]]$NodeIds = @(),
    [string[]]$Signatures = @(),
    [string]$TicketId,
    [string]$Mode = 'skeleton',
    [int]$MaxNodes = 6,
    [int]$BudgetTokens = 800,
    [switch]$SkipTicket,
    [string]$BaseUrl = 'http://127.0.0.1:9700',
    [string]$EngineRoot,
    [double]$SinceHours = 72.0,
    [switch]$Apply,
    [switch]$EnsureExistingEdges
)

$ErrorActionPreference = 'Stop'
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $Utf8NoBom
$OutputEncoding = $Utf8NoBom
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

$AgentlessActions = @('doctor', 'pr-check', 'pr-create')
if ($AgentlessActions -notcontains $Action) {
    if ([string]::IsNullOrWhiteSpace($AgentId)) {
        throw "$Action requires an explicit unique -AgentId."
    }
    $AgentId = $AgentId.Trim()
    if ([string]::Equals($AgentId, 'codex-main', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Generic AgentId 'codex-main' is not allowed. Use a session- or workorder-specific id such as 'codex-main-<session-or-workorder>'."
    }
}

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Helper = Join-Path $ProjectRoot 'scripts\3can_codex.py'
$Wrapper = Join-Path $ProjectRoot 'scripts\3can_codex_wrapper.ps1'
$PrHarness = Join-Path $ProjectRoot 'scripts\3can_pr_harness.py'

function Resolve-Codex3CanEngineRoot {
    if ($EngineRoot) {
        return [System.IO.Path]::GetFullPath($EngineRoot)
    }
    if ($env:THREECAN_ENGINE_ROOT) {
        return [System.IO.Path]::GetFullPath($env:THREECAN_ENGINE_ROOT)
    }
    $capsulePath = Join-Path $ProjectRoot '.agents\project.json'
    if (-not (Test-Path -LiteralPath $capsulePath)) {
        return $null
    }
    try {
        $capsule = [System.IO.File]::ReadAllText($capsulePath, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
        $configured = if ($capsule.threecan_engine_root) {
            [string]$capsule.threecan_engine_root
        } elseif ($capsule.engine_root) {
            [string]$capsule.engine_root
        } else {
            $null
        }
        if (-not $configured) {
            return $null
        }
        if (-not [System.IO.Path]::IsPathRooted($configured)) {
            $configured = Join-Path $ProjectRoot $configured
        }
        return [System.IO.Path]::GetFullPath($configured)
    } catch {
        throw "Unable to resolve threecan_engine_root from '$capsulePath': $($_.Exception.Message)"
    }
}

$ResolvedEngineRoot = Resolve-Codex3CanEngineRoot
if ($ResolvedEngineRoot) {
    $env:THREECAN_ENGINE_ROOT = $ResolvedEngineRoot
}

function Split-Codex3CanList {
    param([string[]]$Items)
    $splitItems = @()
    foreach ($item in $Items) {
        if (-not $item) {
            continue
        }
        foreach ($part in ($item -split ',')) {
            $trimmed = $part.Trim()
            if ($trimmed) {
                $splitItems += $trimmed
            }
        }
    }
    return $splitItems
}

function Invoke-Codex3CanHelper {
    param([string[]]$HelperArgs)
    & python $Helper @HelperArgs
    if ($LASTEXITCODE -ne 0) {
        throw "3CAN helper failed with exit code $LASTEXITCODE."
    }
}

function Invoke-Codex3CanHelperObject {
    param([string[]]$HelperArgs)
    $output = & python $Helper @HelperArgs
    if ($LASTEXITCODE -ne 0) {
        $joined = $output -join "`n"
        $compact = $null
        try {
            $parsed = $joined | ConvertFrom-Json
            if ($parsed.command -eq 'supervise') {
                $compact = Select-Codex3CanSupervisionSummary -Supervision $parsed
            } else {
                $compact = $parsed
            }
        } catch {
            $compact = $null
        }
        if ($null -ne $compact) {
            $compactJson = $compact | ConvertTo-Json -Depth 20 -Compress
            throw "3CAN helper failed with exit code $LASTEXITCODE. CompactOutput: $compactJson"
        }
        throw "3CAN helper failed with exit code $LASTEXITCODE. Output: $joined"
    }
    if (-not $output) {
        throw '3CAN helper returned empty output.'
    }
    return ($output -join "`n") | ConvertFrom-Json
}

function Invoke-Codex3CanWrapper {
    param([string[]]$WrapperArgs)
    & $Wrapper @WrapperArgs
    if ($LASTEXITCODE -ne 0) {
        throw "3CAN wrapper failed with exit code $LASTEXITCODE."
    }
}

function Invoke-Codex3CanWrapperObject {
    param([string[]]$WrapperArgs)
    $output = & $Wrapper @WrapperArgs
    if ($LASTEXITCODE -ne 0) {
        $joined = $output -join "`n"
        throw "3CAN wrapper failed with exit code $LASTEXITCODE. Output: $joined"
    }
    if (-not $output) {
        throw '3CAN wrapper returned empty output.'
    }
    return ($output -join "`n") | ConvertFrom-Json
}

function Select-Codex3CanGateSummary {
    param([object[]]$Gates)
    $summaries = @()
    foreach ($gate in $Gates) {
        $summary = [ordered]@{
            name = $gate.name
            status = $gate.status
        }
        if ($gate.warning) {
            $summary.warning = $gate.warning
        }
        if ($gate.error) {
            $summary.error = $gate.error
        }
        if ($gate.ticket_id) {
            $summary.ticket_id = $gate.ticket_id
        }
        if ($gate.route_token_estimate) {
            $summary.route_token_estimate = $gate.route_token_estimate
        }
        $summaries += [pscustomobject]$summary
    }
    return $summaries
}

function Select-Codex3CanSupervisionSummary {
    param([object]$Supervision)
    return [ordered]@{
        ok = $Supervision.ok
        status = $Supervision.supervision_status
        task_description = $Supervision.task_description
        target_files = $Supervision.target_files
        scope_keywords = $Supervision.scope_keywords
        ticket_mode = $Supervision.ticket_mode
        ticket_id = $Supervision.ticket_id
        state_path = $Supervision.supervision_state_path
        gates = Select-Codex3CanGateSummary -Gates $Supervision.gates
        route_node_ids = @($Supervision.route.nodes | ForEach-Object { $_.id })
    }
}

function Select-Codex3CanPrepareSummary {
    param([object]$PrepareResult)
    return [ordered]@{
        ticket_id = $PrepareResult.ticket.ticket_id
        ticket_valid = $PrepareResult.ticket_status.valid
        remaining_ttl_sec = $PrepareResult.ticket_status.remaining_ttl_sec
        consume_ok = $PrepareResult.consume.response.ok
        consume_count = $PrepareResult.consume.response.consume_count
        memory_status = $PrepareResult.memory_preflight.status
        memory_quality = $PrepareResult.memory_preflight.memory_quality
        wrapper_state_path = $PrepareResult.wrapper_state_path
    }
}

function Select-Codex3CanSuperviseStatusSummary {
    param([object]$StatusResult)
    return [ordered]@{
        valid = $StatusResult.valid
        age_sec = $StatusResult.age_sec
        ttl_sec = $StatusResult.ttl_sec
        supervision_status = $StatusResult.state.supervision_status
        gate_statuses = $StatusResult.state.gate_statuses
        target_files = $StatusResult.state.target_files
        ticket_mode = $StatusResult.state.ticket_mode
    }
}

function Select-Codex3CanDoneSummary {
    param([object]$DoneResult)
    return [ordered]@{
        ok = $DoneResult.ok
        status = $DoneResult.status
        action = $DoneResult.action
        ticket_id_used = $DoneResult.ticket_id_used
        ticket_valid = $DoneResult.ticket_status.valid
        remaining_ttl_sec = $DoneResult.ticket_status.remaining_ttl_sec
        invalid_cached_ticket_cleared = $DoneResult.invalid_cached_ticket_cleared
        affected_nodes = $DoneResult.affected_nodes
    }
}

$ResolvedTargetFiles = Split-Codex3CanList $TargetFiles
$ResolvedScopeKeywords = Split-Codex3CanList $ScopeKeywords
$ResolvedAffectedNodes = Split-Codex3CanList $AffectedNodes
$ResolvedErrors = Split-Codex3CanList $ResolvedErrors
$ResolvedErrorDispositions = @(
    $ErrorDispositions |
        ForEach-Object { if ($_ -ne $null) { $_.Trim() } } |
        Where-Object { $_ }
)
$ResolvedVerificationEvidence = @(
    $VerificationEvidence |
        ForEach-Object { if ($_ -ne $null) { $_.Trim() } } |
        Where-Object { $_ }
)
$ResolvedNextSteps = Split-Codex3CanList $NextSteps
$ResolvedRelatedNodes = Split-Codex3CanList $RelatedNodes
$ResolvedNodeIds = Split-Codex3CanList $NodeIds
$ResolvedSignatures = Split-Codex3CanList $Signatures

switch ($Action) {
    'doctor' {
        Invoke-Codex3CanHelper -HelperArgs @('--base-url', $BaseUrl, 'doctor')
    }

    'bootstrap' {
        $sessionArgs = @(
            '--base-url', $BaseUrl,
            'session-start',
            '--agent-id', $AgentId,
            '--name', 'Codex CLI',
            '--role', $Role,
            '--task', $Task,
            '--max-nodes', "$MaxNodes",
            '--capability', 'code',
            '--capability', $Role,
            '--capability', '3can'
        )
        $session = Invoke-Codex3CanHelperObject -HelperArgs $sessionArgs
        $route = Invoke-Codex3CanHelperObject -HelperArgs @(
            '--base-url', $BaseUrl,
            'route',
            '--agent-id', $AgentId,
            '--task', $Task,
            '--mode', $Mode,
            '--max-nodes', "$MaxNodes",
            '--budget-tokens', "$BudgetTokens",
            '--timeout-seconds', '90',
            '--allow-degraded'
        )
        [ordered]@{
            ok = $true
            action = 'bootstrap'
            agent_id = $AgentId
            role = $Role
            task = $Task
            session = $session
            route = $route
            next_commands = @(
                "scripts\codex-3can.cmd prepare -AgentId $AgentId -TaskDescription `"edit focused area`" -TargetFiles path/to/file -ToolName apply_patch -ToolInputSummary `"edit focused area`"",
                "scripts\codex-3can.cmd done -AgentId $AgentId -TicketId <ticket-id> -Detail `"what changed and why`"",
                "scripts\codex-3can.cmd compact -AgentId $AgentId -TaskSummary `"handoff summary`"",
                "For GitHub PR creation: scripts\codex-3can.cmd pr-check -GitWorktree <worktree>; then scripts\codex-3can.cmd pr-create -GitWorktree <worktree> -HeadBranch <branch> -BaseBranch <base> -Title `<title`> -Body `<body`> -ApprovalId <approval>"
            )
        } | ConvertTo-Json -Depth 10
    }

    'start' {
        & $Wrapper start -AgentId $AgentId -Role $Role -Task $Task -MaxNodes $MaxNodes -BaseUrl $BaseUrl
    }

    'route' {
        Invoke-Codex3CanHelper -HelperArgs @(
            '--base-url', $BaseUrl,
            'route',
            '--agent-id', $AgentId,
            '--task', $Task,
            '--mode', $Mode,
            '--max-nodes', "$MaxNodes",
            '--budget-tokens', "$BudgetTokens",
            '--timeout-seconds', '90',
            '--allow-degraded'
        )
    }

    'supervise' {
        if (-not $TaskDescription) {
            throw 'supervise requires -TaskDescription.'
        }
        $args = @(
            '--base-url', $BaseUrl,
            'supervise',
            '--agent-id', $AgentId,
            '--task-description', $TaskDescription,
            '--task-type', 'Edit',
            '--tool-name', $ToolName,
            '--mode', $Mode,
            '--max-nodes', "$MaxNodes",
            '--budget-tokens', "$BudgetTokens",
            '--timeout-seconds', '90'
        )
        if ($ToolInputSummary) {
            $args += @('--tool-input-summary', $ToolInputSummary)
        }
        if ($TicketId) {
            $args += @('--ticket-id', $TicketId)
        }
        if ($SkipTicket) {
            $args += @('--skip-ticket')
        }
        foreach ($path in $ResolvedTargetFiles) {
            $args += @('--target-file', $path)
        }
        foreach ($keyword in $ResolvedScopeKeywords) {
            $args += @('--scope-keyword', $keyword)
        }
        Invoke-Codex3CanHelper -HelperArgs $args
    }

    'supervise-status' {
        $args = @(
            '--base-url', $BaseUrl,
            'supervise-status',
            '--agent-id', $AgentId
        )
        if ($TaskDescription) {
            $args += @('--expect-scope-text', $TaskDescription)
        }
        foreach ($path in $ResolvedTargetFiles) {
            $args += @('--expect-target-file', $path)
        }
        Invoke-Codex3CanHelper -HelperArgs $args
    }

    'prepare' {
        if (-not $TaskDescription) {
            throw 'prepare requires -TaskDescription.'
        }
        if (-not $ResolvedTargetFiles -or $ResolvedTargetFiles.Count -eq 0) {
            throw 'prepare requires at least one -TargetFiles entry.'
        }
        if (-not $ToolInputSummary) {
            throw 'prepare requires -ToolInputSummary.'
        }
        $superviseArgs = @(
            '--base-url', $BaseUrl,
            'supervise',
            '--agent-id', $AgentId,
            '--task-description', $TaskDescription,
            '--task-type', 'Edit',
            '--tool-name', $ToolName,
            '--tool-input-summary', $ToolInputSummary,
            '--mode', $Mode,
            '--max-nodes', "$MaxNodes",
            '--budget-tokens', "$BudgetTokens",
            '--timeout-seconds', '90',
            '--skip-ticket'
        )
        foreach ($path in $ResolvedTargetFiles) {
            $superviseArgs += @('--target-file', $path)
        }
        foreach ($keyword in $ResolvedScopeKeywords) {
            $superviseArgs += @('--scope-keyword', $keyword)
        }
        $supervision = Invoke-Codex3CanHelperObject -HelperArgs $superviseArgs

        $prepareArgs = @(
            'prepare-mutate',
            '-AgentId', $AgentId,
            '-TaskDescription', $TaskDescription,
            '-TargetFiles', ($ResolvedTargetFiles -join ',')
        )
        if ($ResolvedScopeKeywords -and $ResolvedScopeKeywords.Count -gt 0) {
            $prepareArgs += @('-ScopeKeywords', ($ResolvedScopeKeywords -join ','))
        }
        $prepareArgs += @(
            '-ToolName', $ToolName,
            '-ToolInputSummary', $ToolInputSummary,
            '-BaseUrl', $BaseUrl
        )
        $prepareResult = Invoke-Codex3CanWrapperObject -WrapperArgs $prepareArgs

        [ordered]@{
            ok = $true
            action = 'prepare'
            supervision = Select-Codex3CanSupervisionSummary -Supervision $supervision
            prepare = Select-Codex3CanPrepareSummary -PrepareResult $prepareResult
        } | ConvertTo-Json -Depth 30
    }

    'fail' {
        if (-not $CommandSummary) {
            throw 'fail requires -CommandSummary.'
        }
        if (-not $ErrorExcerpt) {
            throw 'fail requires -ErrorExcerpt.'
        }
        $failArgs = @(
            '--base-url', $BaseUrl,
            'fail',
            '--agent-id', $AgentId,
            '--command-summary', $CommandSummary,
            '--error-excerpt', $ErrorExcerpt
        )
        if ($Diagnosis) {
            $failArgs += @('--diagnosis', $Diagnosis)
        }
        if ($OperationClass) {
            $failArgs += @('--operation-class', $OperationClass)
        }
        if ($Component) {
            $failArgs += @('--component', $Component)
        }
        if ($ErrorType) {
            $failArgs += @('--error-type', $ErrorType)
        }
        if ($RootCause) {
            $failArgs += @('--root-cause', $RootCause)
        }
        foreach ($path in $ResolvedTargetFiles) {
            $failArgs += @('--target-file', $path)
        }
        foreach ($keyword in $ResolvedScopeKeywords) {
            $failArgs += @('--scope-keyword', $keyword)
        }
        foreach ($node in $ResolvedRelatedNodes) {
            $failArgs += @('--related-node', $node)
        }
        Invoke-Codex3CanHelper -HelperArgs $failArgs
    }

    'failure-gate-sync' {
        $syncArgs = @(
            '--base-url', $BaseUrl,
            'failure-gate-sync',
            '--agent-id', $AgentId,
            '--since-hours', ([string]$SinceHours)
        )
        foreach ($signature in $ResolvedSignatures) {
            $syncArgs += @('--signature', $signature)
        }
        foreach ($nodeId in $ResolvedNodeIds) {
            $syncArgs += @('--node-id', $nodeId)
        }
        if ($Apply) {
            $syncArgs += @('--apply')
        }
        if ($EnsureExistingEdges) {
            $syncArgs += @('--ensure-existing-edges')
        }
        Invoke-Codex3CanHelper -HelperArgs $syncArgs
    }

    'done' {
        if (-not $Detail) {
            throw 'done requires -Detail.'
        }
        if (-not $TicketId) {
            throw 'done requires explicit -TicketId; shared wrapper state is never inferred.'
        }
        $doneArgs = @(
            'after-edit',
            '-AgentId', $AgentId,
            '-Detail', $Detail,
            '-Action', 'done'
        )
        if ($ResolvedAffectedNodes -and $ResolvedAffectedNodes.Count -gt 0) {
            $doneArgs += @('-AffectedNodes', ($ResolvedAffectedNodes -join ','))
        }
        if ($ResolvedTargetFiles -and $ResolvedTargetFiles.Count -gt 0) {
            $doneArgs += @('-TargetFiles', ($ResolvedTargetFiles -join ','))
        }
        if ($TicketId) {
            $doneArgs += @('-TicketId', $TicketId)
        }
        if ($ResolvedErrors -and $ResolvedErrors.Count -gt 0) {
            $doneArgs += @('-ResolvedErrors', ($ResolvedErrors -join ','))
        }
        if ($ResolvedErrorDispositions -and $ResolvedErrorDispositions.Count -gt 0) {
            $dispositionItems = [System.Collections.Generic.List[object]]::new()
            foreach ($rawDisposition in $ResolvedErrorDispositions) {
                try {
                    $parsedDisposition = $rawDisposition | ConvertFrom-Json -ErrorAction Stop
                } catch {
                    throw "ErrorDispositions must contain valid JSON objects or arrays: $($_.Exception.Message)"
                }
                if ($parsedDisposition -is [System.Array]) {
                    foreach ($item in $parsedDisposition) {
                        $dispositionItems.Add($item)
                    }
                } else {
                    $dispositionItems.Add($parsedDisposition)
                }
            }
            $dispositionJson = ConvertTo-Json -InputObject @($dispositionItems) -Depth 12 -Compress
            $doneArgs += @('-ErrorDispositions', $dispositionJson)
        }
        if ($SolutionSummary) {
            $doneArgs += @('-SolutionSummary', $SolutionSummary)
        }
        if ($RootCause) {
            $doneArgs += @('-RootCause', $RootCause)
        }
        if ($ResolvedVerificationEvidence -and $ResolvedVerificationEvidence.Count -gt 0) {
            $evidenceItems = [System.Collections.Generic.List[object]]::new()
            foreach ($rawEvidence in $ResolvedVerificationEvidence) {
                try {
                    $parsedEvidence = $rawEvidence | ConvertFrom-Json -ErrorAction Stop
                } catch {
                    throw "VerificationEvidence must contain valid JSON objects or arrays: $($_.Exception.Message)"
                }
                if ($parsedEvidence -is [System.Array]) {
                    foreach ($item in $parsedEvidence) {
                        $evidenceItems.Add($item)
                    }
                } else {
                    $evidenceItems.Add($parsedEvidence)
                }
            }
            $evidenceJson = ConvertTo-Json -InputObject @($evidenceItems) -Depth 30 -Compress
            $doneArgs += @('-VerificationEvidence', $evidenceJson)
        }
        if ($FixedIn) {
            $doneArgs += @('-FixedIn', $FixedIn)
        }
        $doneArgs += @('-BaseUrl', $BaseUrl)
        $doneResult = Invoke-Codex3CanWrapperObject -WrapperArgs $doneArgs

        # Completion is authorized by the backend's durable consumed-ticket
        # receipt. A stale local supervise TTL is informative only and must not
        # prevent a valid expired-consumed ticket from completing.
        (Select-Codex3CanDoneSummary -DoneResult $doneResult) | ConvertTo-Json -Depth 30
    }

    'compact' {
        if (-not $TaskSummary) {
            throw 'compact requires -TaskSummary.'
        }
        & $Wrapper before-compact `
            -AgentId $AgentId `
            -Title $Title `
            -TaskSummary $TaskSummary `
            -TargetFiles $ResolvedTargetFiles `
            -NextSteps $ResolvedNextSteps `
            -RelatedNodes $ResolvedRelatedNodes `
            -TicketId $TicketId `
            -BaseUrl $BaseUrl
    }

    'state' {
        & $Wrapper show-state -AgentId $AgentId -BaseUrl $BaseUrl
    }

    'clear' {
        & $Wrapper clear-state -AgentId $AgentId -BaseUrl $BaseUrl
    }

    'pr-check' {
        $cwd = if ($GitWorktree) { $GitWorktree } else { $ProjectRoot }
        & python $PrHarness check --cwd $cwd --check-token
        if ($LASTEXITCODE -ne 0) {
            throw "3CAN PR harness check failed with exit code $LASTEXITCODE."
        }
    }

    'pr-create' {
        if (-not $Title) {
            throw 'pr-create requires -Title.'
        }
        if (-not $ApprovalId) {
            throw 'pr-create requires -ApprovalId. PR creation is an external publish action.'
        }
        $cwd = if ($GitWorktree) { $GitWorktree } else { $ProjectRoot }
        $args = @(
            $PrHarness,
            'create-pr',
            '--cwd', $cwd,
            '--base', $BaseBranch,
            '--title', $Title,
            '--approval-id', $ApprovalId
        )
        if ($HeadBranch) {
            $args += @('--head', $HeadBranch)
        }
        if ($BodyFile) {
            $args += @('--body-file', $BodyFile)
        } else {
            $args += @('--body', $Body)
        }
        & python @args
        if ($LASTEXITCODE -ne 0) {
            throw "3CAN PR harness create-pr failed with exit code $LASTEXITCODE."
        }
    }

}
