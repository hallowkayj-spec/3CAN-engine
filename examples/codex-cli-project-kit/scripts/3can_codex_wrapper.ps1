param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet('doctor', 'start', 'before-edit', 'prepare-mutate', 'before-mutate', 'after-edit', 'before-compact', 'check-ticket')]
    [string]$Command,

    [string]$AgentId,
    [string]$Role = 'frontend',
    [string]$Task = 'codex session',
    [string]$TaskDescription,
    [string[]]$TargetFiles = @(),
    [string[]]$ScopeKeywords = @(),
    [string]$TaskType = 'Edit',
    [string]$ToolName = 'codex-mutate',
    [string]$ToolInputSummary,
    [string]$Title,
    [string]$Detail,
    [string]$Action = 'file_change',
    [string[]]$AffectedNodes = @(),
    [string[]]$ResolvedErrors = @(),
    [string[]]$ErrorDispositions = @(),
    [string]$SolutionSummary,
    [string]$RootCause,
    [string[]]$VerificationEvidence = @(),
    [string]$FixedIn,
    [string]$TaskSummary,
    [string[]]$NextSteps = @(),
    [string[]]$Blockers = @(),
    [string[]]$Files = @(),
    [string[]]$RelatedNodes = @(),
    [string]$TicketId,
    [string]$Meta,
    [int]$MaxNodes = 6,
    [string]$EngineRoot,
    [string]$BaseUrl = 'http://127.0.0.1:9700'
)

$ErrorActionPreference = 'Stop'
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $Utf8NoBom
$OutputEncoding = $Utf8NoBom
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Helper = Join-Path $ProjectRoot 'scripts\3can_codex.py'

if (-not [string]::IsNullOrWhiteSpace($AgentId)) {
    $AgentId = $AgentId.Trim()
    if ([string]::Equals($AgentId, 'codex-main', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Generic AgentId 'codex-main' is not allowed. Let the helper derive the current execution identity or pass a unique id."
    }
}

function Resolve-3CanEngineRoot {
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

$ResolvedEngineRoot = Resolve-3CanEngineRoot
if ($ResolvedEngineRoot) {
    $env:THREECAN_ENGINE_ROOT = $ResolvedEngineRoot
}

function Split-3CanList {
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

function Invoke-HelperJson {
    param(
        [string[]]$HelperArgs
    )
    $output = & python $Helper @HelperArgs
    $exitCode = $LASTEXITCODE
    if (-not $output) {
        throw '3CAN helper returned empty output.'
    }
    if ($exitCode -ne 0) {
        $joined = $output -join "`n"
        throw "3CAN helper failed with exit code $exitCode. Output: $joined"
    }
    return ($output -join "`n") | ConvertFrom-Json
}

function Invoke-HelperJsonAllowFailure {
    param(
        [string[]]$HelperArgs
    )
    $output = & python $Helper @HelperArgs
    $exitCode = $LASTEXITCODE
    if (-not $output) {
        throw '3CAN helper returned empty output.'
    }
    $joined = $output -join "`n"
    return [ordered]@{
        exit_code = $exitCode
        json = $joined | ConvertFrom-Json
        raw = $joined
    }
}

$TargetFiles = Split-3CanList $TargetFiles
$ScopeKeywords = Split-3CanList $ScopeKeywords
$AffectedNodes = Split-3CanList $AffectedNodes
$ResolvedErrors = Split-3CanList $ResolvedErrors
$ErrorDispositions = @(
    $ErrorDispositions |
        ForEach-Object { if ($_ -ne $null) { $_.Trim() } } |
        Where-Object { $_ }
)
$VerificationEvidence = @(
    $VerificationEvidence |
        ForEach-Object { if ($_ -ne $null) { $_.Trim() } } |
        Where-Object { $_ }
)
$NextSteps = Split-3CanList $NextSteps
$Blockers = Split-3CanList $Blockers
$Files = Split-3CanList $Files
$RelatedNodes = Split-3CanList $RelatedNodes

function Issue-RouteTicket {
    param(
        [string]$CurrentAgentId,
        [string]$CurrentBaseUrl,
        [string]$CurrentTaskDescription,
        [string[]]$CurrentTargetFiles,
        [string[]]$CurrentScopeKeywords,
        [string]$CurrentTaskType
    )

    if (-not $CurrentTaskDescription) {
        throw 'Task description is required.'
    }
    if (-not $CurrentTargetFiles -or $CurrentTargetFiles.Count -eq 0) {
        throw 'At least one target file is required.'
    }

    $args = @(
        '--base-url', $CurrentBaseUrl,
        'ticket',
        '--agent-id', $CurrentAgentId,
        '--task-description', $CurrentTaskDescription,
        '--task-type', $CurrentTaskType
    )
    foreach ($path in $CurrentTargetFiles) {
        $args += @('--target-file', $path)
    }
    foreach ($keyword in $CurrentScopeKeywords) {
        $args += @('--scope-keyword', $keyword)
    }
    return Invoke-HelperJson $args
}

function Resolve-TicketContext {
    param(
        [string]$CurrentAgentId,
        [string]$CurrentBaseUrl,
        [string]$ExplicitTicketId,
        [string]$ExpectedScopeText,
        [string[]]$ExpectedTargetFiles = @(),
        [switch]$RequireLiveTicket
    )

    if (-not $ExplicitTicketId) {
        if ($RequireLiveTicket) {
            throw 'An explicit -TicketId is required; wrapper ticket state is not inferred.'
        }
        return [ordered]@{
            ticket_id = $null
            status = $null
        }
    }

    $statusArgs = @(
        '--base-url', $CurrentBaseUrl,
        'ticket-status',
        '--ticket-id', $ExplicitTicketId,
        '--expect-agent-id', $CurrentAgentId
    )
    if ($ExpectedScopeText) {
        $statusArgs += @('--expect-scope-text', $ExpectedScopeText)
    }
    foreach ($path in $ExpectedTargetFiles) {
        if ($path) {
            $statusArgs += @('--expect-target-file', $path)
        }
    }
    if ($RequireLiveTicket) {
        $statusArgs += @('--min-remaining-ttl-sec', '5')
    }
    $statusResult = Invoke-HelperJsonAllowFailure $statusArgs
    $status = $statusResult.json
    if ($RequireLiveTicket -and ($statusResult.exit_code -ne 0 -or -not $status.valid)) {
        $errorMessage = if ($status.error) { ($status.error | ConvertTo-Json -Compress) } else { 'unknown ticket validation error' }
        throw "Route ticket '$ExplicitTicketId' is not valid against live 3CAN. $errorMessage"
    }

    return [ordered]@{
        ticket_id = $ExplicitTicketId
        status = $status
    }
}

switch ($Command) {
    'doctor' {
        $result = Invoke-HelperJson @('--base-url', $BaseUrl, 'doctor')
        $result | ConvertTo-Json -Depth 6
    }

    'start' {
        $args = @(
            '--base-url', $BaseUrl,
            'session-start',
            '--agent-id', $AgentId,
            '--name', 'Codex CLI',
            '--role', $Role,
            '--task', $Task,
            '--max-nodes', "$MaxNodes"
        )
        $args += @('--capability', 'code', '--capability', 'frontend', '--capability', '3can')
        $result = Invoke-HelperJson $args
        $result | ConvertTo-Json -Depth 6
    }

    'before-edit' {
        if (-not $TaskDescription) {
            throw 'before-edit requires -TaskDescription.'
        }
        if (-not $TargetFiles -or $TargetFiles.Count -eq 0) {
            throw 'before-edit requires at least one -TargetFiles entry.'
        }
        $issued = Issue-RouteTicket -CurrentAgentId $AgentId -CurrentBaseUrl $BaseUrl -CurrentTaskDescription $TaskDescription -CurrentTargetFiles $TargetFiles -CurrentScopeKeywords $ScopeKeywords -CurrentTaskType $TaskType
        $issued | ConvertTo-Json -Depth 6
    }

    'prepare-mutate' {
        if (-not $TaskDescription) {
            throw 'prepare-mutate requires -TaskDescription.'
        }
        if (-not $TargetFiles -or $TargetFiles.Count -eq 0) {
            throw 'prepare-mutate requires at least one -TargetFiles entry.'
        }
        if (-not $ToolInputSummary) {
            throw 'prepare-mutate requires -ToolInputSummary.'
        }
        $args = @(
            '--base-url', $BaseUrl,
            'prepare',
            '--agent-id', $AgentId,
            '--task-description', $TaskDescription,
            '--task-type', $TaskType,
            '--tool-name', $ToolName,
            '--tool-input-summary', $ToolInputSummary
        )
        foreach ($path in $TargetFiles) {
            $args += @('--target-file', $path)
        }
        foreach ($keyword in $ScopeKeywords) {
            $args += @('--scope-keyword', $keyword)
        }
        $result = Invoke-HelperJson $args
        $result | ConvertTo-Json -Depth 8
    }

    'before-mutate' {
        if (-not $ToolInputSummary) {
            throw 'before-mutate requires -ToolInputSummary.'
        }
        if (-not $TicketId) {
            throw 'before-mutate requires explicit -TicketId.'
        }
        $args = @(
            '--base-url', $BaseUrl,
            'ticket-consume',
            '--ticket-id', $TicketId,
            '--agent-id', $AgentId,
            '--tool-name', $ToolName,
            '--tool-input-summary', $ToolInputSummary
        )
        $result = Invoke-HelperJson $args
        $result | ConvertTo-Json -Depth 8
    }

    'after-edit' {
        if (-not $Detail) {
            throw 'after-edit requires -Detail.'
        }
        if ($Action -eq 'done' -and -not $TicketId) {
            throw 'done requires explicit -TicketId; shared wrapper state is never inferred.'
        }
        $resolvedTicketId = $TicketId
        if ($Action -eq 'done') {
            $args = @(
                '--base-url', $BaseUrl,
                'done',
                '--agent-id', $AgentId,
                '--detail', $Detail
            )
        } else {
            $args = @(
                '--base-url', $BaseUrl,
                'activity-log',
                '--agent-id', $AgentId,
                '--action', $Action,
                '--detail', $Detail
            )
        }
        foreach ($nodeId in $AffectedNodes) {
            $args += @('--affected-node', $nodeId)
        }
        if ($resolvedTicketId) {
            $args += @('--ticket-id', $resolvedTicketId)
        }
        if ($Meta) {
            $args += @('--meta', $Meta)
        }
        if ($Action -eq 'done') {
            foreach ($nodeId in $ResolvedErrors) {
                $args += @('--resolved-error', $nodeId)
            }
            foreach ($disposition in $ErrorDispositions) {
                $args += @('--error-disposition', $disposition)
            }
            if ($SolutionSummary) {
                $args += @('--solution-summary', $SolutionSummary)
            }
            if ($RootCause) {
                $args += @('--root-cause', $RootCause)
            }
            foreach ($receipt in $VerificationEvidence) {
                $args += @('--verification-evidence', $receipt)
            }
            if ($FixedIn) {
                $args += @('--fixed-in', $FixedIn)
            }
        }
        $result = Invoke-HelperJson $args
        $result | Add-Member -NotePropertyName action -NotePropertyValue $Action -Force
        $result | Add-Member -NotePropertyName affected_nodes -NotePropertyValue $AffectedNodes -Force
        $result | Add-Member -NotePropertyName ticket_id_used -NotePropertyValue $resolvedTicketId
        $result | ConvertTo-Json -Depth 6
    }

    'before-compact' {
        if (-not $TaskSummary) {
            throw 'before-compact requires -TaskSummary.'
        }
        $compactFiles = @()
        foreach ($item in $Files) {
            if ($item -and ($compactFiles -notcontains $item)) {
                $compactFiles += $item
            }
        }
        foreach ($item in $TargetFiles) {
            if ($item -and ($compactFiles -notcontains $item)) {
                $compactFiles += $item
            }
        }
        $args = @(
            '--base-url', $BaseUrl,
            'compact-note',
            '--agent-id', $AgentId,
            '--task-summary', $TaskSummary
        )
        if ($Title) {
            $args += @('--title', $Title)
        }
        foreach ($item in $NextSteps) {
            $args += @('--next-step', $item)
        }
        foreach ($item in $Blockers) {
            $args += @('--blocker', $item)
        }
        foreach ($item in $compactFiles) {
            $args += @('--file', $item)
        }
        foreach ($item in $RelatedNodes) {
            $args += @('--related-node', $item)
        }
        $result = Invoke-HelperJson $args
        $result | Add-Member -NotePropertyName compact_scope_files -NotePropertyValue $compactFiles
        $result | Add-Member -NotePropertyName compact_scope_selection -NotePropertyValue 'explicit_files_only'
        $result | ConvertTo-Json -Depth 8
    }

    'check-ticket' {
        $ticketContext = Resolve-TicketContext -CurrentAgentId $AgentId -CurrentBaseUrl $BaseUrl -ExplicitTicketId $TicketId -RequireLiveTicket
        [ordered]@{
            ticket_id = $ticketContext.ticket_id
            status = $ticketContext.status
        } | ConvertTo-Json -Depth 8
    }

}
