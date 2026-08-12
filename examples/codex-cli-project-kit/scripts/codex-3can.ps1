[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateScript({
        $unsupportedPackagedActions = @('deploy', 'maintenance', 'autoloop')
        if ($unsupportedPackagedActions -contains $_) {
            throw "Action '$_' is unsupported in this release package because its implementation harness is not shipped."
        }
        $supportedPackagedActions = @(
            'doctor', 'bootstrap', 'start', 'route', 'prepare',
            'done', 'compact'
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
    [string[]]$NextSteps = @(),
    [string[]]$RelatedNodes = @(),
    [string]$TicketId,
    [string]$Mode = 'skeleton',
    [int]$MaxNodes = 6,
    [int]$BudgetTokens = 800,
    [string]$BaseUrl = 'http://127.0.0.1:9700',
    [string]$EngineRoot
)

$ErrorActionPreference = 'Stop'
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $Utf8NoBom
$OutputEncoding = $Utf8NoBom
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

if (-not [string]::IsNullOrWhiteSpace($AgentId)) {
    $AgentId = $AgentId.Trim()
    if ([string]::Equals($AgentId, 'codex-main', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Generic AgentId 'codex-main' is not allowed. Let the helper derive the current execution identity or pass a unique id."
    }
}

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Helper = Join-Path $ProjectRoot 'scripts\3can_codex.py'
$Wrapper = Join-Path $ProjectRoot 'scripts\3can_codex_wrapper.ps1'

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
            $compact = $joined | ConvertFrom-Json
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

function Invoke-Codex3CanWrapperObject {
    param([string[]]$WrapperArgs)
    if (-not $WrapperArgs -or (($WrapperArgs.Count - 1) % 2) -ne 0) {
        throw '3CAN wrapper arguments must be one command plus exact name/value pairs.'
    }
    $command = $WrapperArgs[0]
    $parameters = @{}
    for ($index = 1; $index -lt $WrapperArgs.Count; $index += 2) {
        $name = ([string]$WrapperArgs[$index]).TrimStart('-')
        if (-not $name) {
            throw '3CAN wrapper parameter name must not be empty.'
        }
        $parameters[$name] = $WrapperArgs[$index + 1]
    }
    $output = & $Wrapper $command @parameters
    if ($LASTEXITCODE -ne 0) {
        $joined = $output -join "`n"
        throw "3CAN wrapper failed with exit code $LASTEXITCODE. Output: $joined"
    }
    if (-not $output) {
        throw '3CAN wrapper returned empty output.'
    }
    return ($output -join "`n") | ConvertFrom-Json
}

function Select-Codex3CanPrepareSummary {
    param([object]$PrepareResult)
    return [ordered]@{
        ticket_id = $PrepareResult.ticket.ticket_id
        ticket_state = $PrepareResult.ticket.state
        ttl_sec = $PrepareResult.ticket.ttl_sec
        consume_ok = $PrepareResult.consume.ok
        consume_count = $PrepareResult.consume.consume_count
    }
}

function Select-Codex3CanDoneSummary {
    param([object]$DoneResult)
    return [ordered]@{
        ok = $DoneResult.ok
        status = $DoneResult.status
        action = $DoneResult.action
        ticket_id_used = $DoneResult.ticket_id_used
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
            agent_id = $session.checkin.agent_id
            role = $Role
            task = $Task
            session = $session
            route = $route
            next_commands = @(
                "scripts\codex-3can.cmd prepare -TaskDescription `"edit focused area`" -TargetFiles path/to/file -ToolName apply_patch -ToolInputSummary `"edit focused area`"",
                "scripts\codex-3can.cmd done -TicketId <ticket-id> -Detail `"what changed and why`"",
                "scripts\codex-3can.cmd compact -TaskSummary `"handoff summary`""
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
            prepare = Select-Codex3CanPrepareSummary -PrepareResult $prepareResult
        } | ConvertTo-Json -Depth 30
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

        # Completion integrity is bound by the backend's durable consumed-ticket
        # receipt; the wrapper carries the exact TicketId without local policy.
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

}
