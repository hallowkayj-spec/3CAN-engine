param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet('doctor', 'start', 'before-edit', 'prepare-mutate', 'before-mutate', 'after-edit', 'before-compact', 'check-ticket', 'show-state', 'clear-state')]
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

if ($Command -ne 'doctor') {
    if ([string]::IsNullOrWhiteSpace($AgentId)) {
        throw "$Command requires an explicit unique -AgentId."
    }
    $AgentId = $AgentId.Trim()
    if ([string]::Equals($AgentId, 'codex-main', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Generic AgentId 'codex-main' is not allowed. Use a session- or workorder-specific id such as 'codex-main-<session-or-workorder>'."
    }
}

function Get-3CanSha256Text {
    param([string]$Text)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($Text)
        return -join ($sha.ComputeHash($bytes) | ForEach-Object { $_.ToString('x2') })
    } finally {
        $sha.Dispose()
    }
}

function Resolve-3CanWorktreeRoot {
    if (Get-Command git -ErrorAction SilentlyContinue) {
        try {
            $gitRoot = & git -C $ProjectRoot rev-parse --show-toplevel 2>$null
            if ($LASTEXITCODE -eq 0 -and $gitRoot) {
                return [System.IO.Path]::GetFullPath([string]($gitRoot | Select-Object -First 1))
            }
        } catch {
            # A non-Git project safely falls back to its project root below.
        }
    }
    return [System.IO.Path]::GetFullPath($ProjectRoot)
}

function Normalize-3CanWorktreeRoot {
    param([string]$Path)
    $normalized = [System.IO.Path]::GetFullPath($Path).Replace('\', '/').TrimEnd('/')
    if ([System.IO.Path]::DirectorySeparatorChar -eq '\') {
        return $normalized.ToLowerInvariant()
    }
    return $normalized
}

$WorktreeRoot = Resolve-3CanWorktreeRoot
$NormalizedWorktreeRoot = Normalize-3CanWorktreeRoot $WorktreeRoot
$WorktreeRootSha256 = Get-3CanSha256Text $NormalizedWorktreeRoot
$WorkspaceKey = $WorktreeRootSha256.Substring(0, 16)
$AgentStateKey = if ($AgentId) {
    $safePrefix = [regex]::Replace($AgentId.ToLowerInvariant(), '[^a-z0-9._-]', '_')
    if ($safePrefix.Length -gt 48) {
        $safePrefix = $safePrefix.Substring(0, 48)
    }
    "{0}-{1}" -f $safePrefix, (Get-3CanSha256Text $AgentId).Substring(0, 12)
} else {
    'agentless'
}
$StateDir = Join-Path $ProjectRoot 'test-results\3can'
$AgentWorkspaceStateDir = Join-Path $StateDir ("codex_wrapper_states\{0}\{1}" -f $AgentStateKey, $WorkspaceKey)
$StateIndexPath = Join-Path $AgentWorkspaceStateDir 'index.json'
$ScopedStateDir = Join-Path $AgentWorkspaceStateDir 'scopes'
$ScopedStateIndexLimit = 80
$MaxInheritedStateAgeSec = 900

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

function Normalize-3CanStatePath {
    param([string]$Path)
    if (-not $Path) {
        return ''
    }
    return $Path.Trim().Replace('\', '/').ToLowerInvariant()
}

function Get-3CanScopeTokens {
    param([string]$Text)
    $tokens = @()
    if (-not $Text) {
        return $tokens
    }
    $matches = [regex]::Matches($Text.ToLowerInvariant(), '[a-z0-9][a-z0-9_.-]{2,}|[\u4e00-\u9fff]{2,}')
    $ignored = @('3can', 'codex', 'runtime', 'harness', 'wrapper', 'task', 'edit', 'done', 'change', 'changes', 'file', 'files', 'update', 'updated', 'fix', 'fixed')
    foreach ($match in $matches) {
        $value = $match.Value
        if ($ignored -notcontains $value -and $tokens -notcontains $value) {
            $tokens += $value
        }
    }
    return $tokens
}

function Get-3CanScopeKey {
    param(
        [string]$CurrentAgentId,
        [string]$CurrentBaseUrl,
        [string]$CurrentTaskDescription,
        [string[]]$CurrentTargetFiles = @(),
        [string[]]$CurrentScopeKeywords = @()
    )
    $material = [ordered]@{
        agent_id = $CurrentAgentId
        workspace_key = $WorkspaceKey
        base_url = $CurrentBaseUrl
        task_tokens = @(Get-3CanScopeTokens $CurrentTaskDescription | Sort-Object | Select-Object -First 16)
        target_files = @($CurrentTargetFiles | ForEach-Object { Normalize-3CanStatePath $_ } | Where-Object { $_ } | Sort-Object -Unique)
        scope_keywords = @($CurrentScopeKeywords | ForEach-Object { $_.Trim().ToLowerInvariant() } | Where-Object { $_ } | Sort-Object -Unique)
    }
    $json = $material | ConvertTo-Json -Compress -Depth 5
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($json)
        $hash = $sha.ComputeHash($bytes)
        $hex = -join ($hash | ForEach-Object { $_.ToString('x2') })
        return $hex.Substring(0, 16)
    } finally {
        $sha.Dispose()
    }
}

function Test-3CanStateFresh {
    param([object]$State)
    if (-not $State -or -not $State.issued_at) {
        return $false
    }
    try {
        $issuedAt = [DateTimeOffset]::Parse([string]$State.issued_at).ToUniversalTime()
        $ticketTtlSec = [int]$State.ttl_sec
        if ($ticketTtlSec -le 0) {
            return $false
        }
        $effectiveTtlSec = [Math]::Min($ticketTtlSec, $MaxInheritedStateAgeSec)
        $ageSec = ([DateTimeOffset]::UtcNow - $issuedAt).TotalSeconds
        return $ageSec -ge -60 -and $ageSec -le $effectiveTtlSec
    } catch {
        return $false
    }
}

function Load-StateIndex {
    if (-not (Test-Path $StateIndexPath)) {
        return [ordered]@{ version = 1; entries = @() }
    }
    try {
        $indexText = [System.IO.File]::ReadAllText($StateIndexPath, [System.Text.Encoding]::UTF8)
        $index = $indexText | ConvertFrom-Json
        if (-not $index.entries) {
            $index | Add-Member -NotePropertyName entries -NotePropertyValue @() -Force
        }
        return $index
    } catch {
        return [ordered]@{ version = 1; entries = @() }
    }
}

function Save-StateIndex {
    param([object]$Index)
    New-Item -ItemType Directory -Force -Path $AgentWorkspaceStateDir | Out-Null
    $json = $Index | ConvertTo-Json -Depth 8
    $tmpPath = Join-Path $AgentWorkspaceStateDir ("index.{0}.{1}.tmp" -f $PID, ([guid]::NewGuid().ToString('N')))
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($tmpPath, $json + [Environment]::NewLine, $utf8NoBom)
    Move-Item -LiteralPath $tmpPath -Destination $StateIndexPath -Force
}

function Get-StateMatchScore {
    param(
        [object]$State,
        [string]$CurrentAgentId,
        [string]$CurrentBaseUrl,
        [string]$ExpectedScopeText,
        [string[]]$ExpectedTargetFiles = @(),
        [string]$ExpectedTicketId
    )
    if ($State.agent_id -and $State.agent_id -ne $CurrentAgentId) {
        return -1
    }
    if (-not $State.workspace_key -or $State.workspace_key -ne $WorkspaceKey) {
        return -1
    }
    if ($State.base_url -and $State.base_url -ne $CurrentBaseUrl) {
        return -1
    }
    $score = 0
    if ($ExpectedTicketId) {
        if ($State.ticket_id -ne $ExpectedTicketId) {
            return -1
        }
        $score += 1000
    }
    $expectedPaths = @($ExpectedTargetFiles | ForEach-Object { Normalize-3CanStatePath $_ } | Where-Object { $_ } | Sort-Object -Unique)
    $statePaths = @($State.target_files | ForEach-Object { Normalize-3CanStatePath $_ } | Where-Object { $_ } | Sort-Object -Unique)
    if ($expectedPaths.Count -gt 0) {
        $missing = @($expectedPaths | Where-Object { $statePaths -notcontains $_ })
        if ($missing.Count -eq 0) {
            $score += 100 + $expectedPaths.Count
        } else {
            $overlap = @($expectedPaths | Where-Object { $statePaths -contains $_ })
            if ($overlap.Count -eq 0) {
                return -1
            }
            $score += 25 + $overlap.Count
        }
    }
    $expectedTokens = @(Get-3CanScopeTokens $ExpectedScopeText)
    if ($expectedTokens.Count -gt 0) {
        $stateText = @(
            $State.task_description
            ($State.scope_keywords -join ' ')
            ($State.target_files -join ' ')
        ) -join ' '
        $stateTokens = @(Get-3CanScopeTokens $stateText)
        $overlapTokens = @($expectedTokens | Where-Object { $stateTokens -contains $_ })
        if ($overlapTokens.Count -gt 0) {
            $score += [Math]::Min($overlapTokens.Count, 20)
        } elseif ($expectedPaths.Count -eq 0 -and -not $ExpectedTicketId) {
            return -1
        }
    }
    return $score
}

function Save-State {
    param([object]$State)
    New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
    New-Item -ItemType Directory -Force -Path $ScopedStateDir | Out-Null
    $scopeKey = $State.scope_key
    if (-not $scopeKey) {
        $scopeKey = Get-3CanScopeKey `
            -CurrentAgentId $State.agent_id `
            -CurrentBaseUrl $State.base_url `
            -CurrentTaskDescription $State.task_description `
            -CurrentTargetFiles $State.target_files `
            -CurrentScopeKeywords $State.scope_keywords
        $State['scope_key'] = $scopeKey
    }
    $scopedPath = Join-Path $ScopedStateDir ("$scopeKey.json")
    $State['state_path'] = $scopedPath
    $json = $State | ConvertTo-Json -Depth 6
    $scopedTmpPath = Join-Path $ScopedStateDir ("{0}.{1}.{2}.tmp" -f $scopeKey, $PID, ([guid]::NewGuid().ToString('N')))
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($scopedTmpPath, $json + [Environment]::NewLine, $utf8NoBom)
    Move-Item -LiteralPath $scopedTmpPath -Destination $scopedPath -Force

    $index = Load-StateIndex
    $entries = @()
    foreach ($entry in @($index.entries)) {
        if ($entry.scope_key -ne $scopeKey) {
            $entries += $entry
        }
    }
    $entries = @(
        [ordered]@{
            scope_key = $scopeKey
            state_path = $scopedPath
            agent_id = $State.agent_id
            workspace_key = $State.workspace_key
            base_url = $State.base_url
            task_description = $State.task_description
            target_files = $State.target_files
            scope_keywords = $State.scope_keywords
            issued_at = $State.issued_at
            ticket_id = $State.ticket_id
        }
    ) + $entries
    $index.entries = @($entries | Select-Object -First $ScopedStateIndexLimit)
    Save-StateIndex $index
}

function Load-State {
    param(
        [string]$CurrentAgentId,
        [string]$CurrentBaseUrl,
        [string]$ExpectedScopeText,
        [string[]]$ExpectedTargetFiles = @(),
        [string]$ExpectedTicketId,
        [bool]$AllowExpiredExactTicket = $false
    )
    $candidates = @()
    $index = Load-StateIndex
    foreach ($entry in @($index.entries)) {
        $path = $entry.state_path
        if (-not $path -or -not (Test-Path $path)) {
            continue
        }
        try {
            $stateText = [System.IO.File]::ReadAllText($path, [System.Text.Encoding]::UTF8)
            $state = $stateText | ConvertFrom-Json
        } catch {
            continue
        }
        $stateFresh = Test-3CanStateFresh $state
        $expiredExactTicket = (
            $AllowExpiredExactTicket -and
            $ExpectedTicketId -and
            $state.ticket_id -eq $ExpectedTicketId
        )
        if (-not $stateFresh -and -not $expiredExactTicket) {
            continue
        }
        $score = Get-StateMatchScore `
            -State $state `
            -CurrentAgentId $CurrentAgentId `
            -CurrentBaseUrl $CurrentBaseUrl `
            -ExpectedScopeText $ExpectedScopeText `
            -ExpectedTargetFiles $ExpectedTargetFiles `
            -ExpectedTicketId $ExpectedTicketId
        if ($score -ge 0) {
            $state | Add-Member -NotePropertyName selection_kind -NotePropertyValue 'scoped_index' -Force
            $state | Add-Member -NotePropertyName selection_score -NotePropertyValue $score -Force
            $state | Add-Member -NotePropertyName state_fresh -NotePropertyValue $stateFresh -Force
            $candidates += $state
        }
    }
    if ($candidates.Count -gt 0) {
        return $candidates | Sort-Object -Property @{Expression='selection_score'; Descending=$true}, @{Expression='issued_at'; Descending=$true} | Select-Object -First 1
    }
    return $null
}

function Clear-TicketState {
    param([object]$State)
    if ($State -and $State.state_path -and (Test-Path $State.state_path)) {
        Remove-Item -LiteralPath $State.state_path -Force
    }
    if ($State -and $State.scope_key -and (Test-Path $StateIndexPath)) {
        $index = Load-StateIndex
        $index.entries = @($index.entries | Where-Object { $_.scope_key -ne $State.scope_key })
        Save-StateIndex $index
    }
    Remove-Item Env:\THREECAN_TICKET_ID -ErrorAction SilentlyContinue
}

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
    $result = Invoke-HelperJson $args
    $state = [ordered]@{
        agent_id = $CurrentAgentId
        workspace_key = $WorkspaceKey
        worktree_root_sha256 = $WorktreeRootSha256
        ticket_id = $result.ticket_id
        task_description = $CurrentTaskDescription
        target_files = $CurrentTargetFiles
        scope_keywords = $CurrentScopeKeywords
        issued_at = $result.issued_at
        ttl_sec = $result.ttl_sec
        base_url = $CurrentBaseUrl
    }
    Save-State $state
    $env:THREECAN_TICKET_ID = $result.ticket_id
    return [ordered]@{
        ticket = $result
        state = $state
    }
}

function Resolve-TicketContext {
    param(
        [string]$CurrentAgentId,
        [string]$CurrentBaseUrl,
        [string]$ExplicitTicketId,
        [string]$ExpectedScopeText,
        [string[]]$ExpectedTargetFiles = @(),
        [switch]$RequireLiveTicket,
        [switch]$AllowInvalidCachedTicket,
        [switch]$RequireWorkspaceBinding,
        [switch]$AllowExpiredExactTicketState
    )

    $state = Load-State `
        -CurrentAgentId $CurrentAgentId `
        -CurrentBaseUrl $CurrentBaseUrl `
        -ExpectedScopeText $ExpectedScopeText `
        -ExpectedTargetFiles $ExpectedTargetFiles `
        -ExpectedTicketId $ExplicitTicketId `
        -AllowExpiredExactTicket $AllowExpiredExactTicketState
    $resolvedTicketId = $ExplicitTicketId
    $resolvedFromCache = $false
    if (-not $resolvedTicketId -and $state) {
        $resolvedTicketId = $state.ticket_id
        $resolvedFromCache = $true
    }
    if (-not $resolvedTicketId -and $env:THREECAN_TICKET_ID) {
        $resolvedTicketId = $env:THREECAN_TICKET_ID
        $resolvedFromCache = $true
    }

    if ($state -and $state.agent_id -and $state.agent_id -ne $CurrentAgentId) {
        throw "Stored wrapper state belongs to agent '$($state.agent_id)', not '$CurrentAgentId'."
    }
    if ($state -and $state.base_url -and $state.base_url -ne $CurrentBaseUrl) {
        throw "Stored wrapper state was created against '$($state.base_url)', not '$CurrentBaseUrl'."
    }
    if (-not $resolvedTicketId) {
        if ($RequireLiveTicket) {
            throw 'No route ticket is available. Run before-edit first or pass -TicketId explicitly.'
        }
        return [ordered]@{
            ticket_id = $null
            state = $state
            status = $null
        }
    }
    if ($RequireWorkspaceBinding) {
        if (-not $state) {
            throw "Ticket '$resolvedTicketId' has no state bound to agent '$CurrentAgentId' in worktree '$WorkspaceKey'."
        }
        if ($state.workspace_key -ne $WorkspaceKey) {
            throw "Stored wrapper state belongs to worktree '$($state.workspace_key)', not '$WorkspaceKey'."
        }
        if ($state.ticket_id -ne $resolvedTicketId) {
            throw "Stored wrapper state is bound to ticket '$($state.ticket_id)', not '$resolvedTicketId'."
        }
    }

    $statusArgs = @(
        '--base-url', $CurrentBaseUrl,
        'ticket-status',
        '--ticket-id', $resolvedTicketId,
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
    if ($RequireLiveTicket -and $statusResult.exit_code -ne 0 -and $AllowInvalidCachedTicket -and $resolvedFromCache -and -not $ExplicitTicketId) {
        Clear-TicketState -State $state
        return [ordered]@{
            ticket_id = $null
            state = $state
            status = $status
            invalid_cached_ticket_cleared = $true
        }
    }
    if ($RequireLiveTicket -and -not $status.valid) {
        $errorMessage = if ($status.error) { ($status.error | ConvertTo-Json -Compress) } else { 'unknown ticket validation error' }
        throw "Route ticket '$resolvedTicketId' is not valid against live 3CAN. $errorMessage"
    }

    return [ordered]@{
        ticket_id = $resolvedTicketId
        state = $state
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
        $issued.ticket | Add-Member -NotePropertyName wrapper_state_path -NotePropertyValue $issued.state.state_path
        $issued.ticket | Add-Member -NotePropertyName wrapper_scoped_state_path -NotePropertyValue $issued.state.state_path
        $issued.ticket | ConvertTo-Json -Depth 6
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
        $preflightArgs = @(
            '--base-url', $BaseUrl,
            'memory-preflight',
            '--agent-id', $AgentId,
            '--task-description', $TaskDescription,
            '--tool-name', $ToolName,
            '--tool-input-summary', $ToolInputSummary
        )
        foreach ($path in $TargetFiles) {
            $preflightArgs += @('--target-file', $path)
        }
        foreach ($keyword in $ScopeKeywords) {
            $preflightArgs += @('--scope-keyword', $keyword)
        }
        $memoryPreflight = Invoke-HelperJson $preflightArgs
        $issued = Issue-RouteTicket -CurrentAgentId $AgentId -CurrentBaseUrl $BaseUrl -CurrentTaskDescription $TaskDescription -CurrentTargetFiles $TargetFiles -CurrentScopeKeywords $ScopeKeywords -CurrentTaskType $TaskType
        $ticketContext = Resolve-TicketContext -CurrentAgentId $AgentId -CurrentBaseUrl $BaseUrl -ExplicitTicketId $issued.ticket.ticket_id -RequireLiveTicket -RequireWorkspaceBinding
        $args = @(
            '--base-url', $BaseUrl,
            'ticket-consume',
            '--ticket-id', $ticketContext.ticket_id,
            '--agent-id', $AgentId,
            '--tool-name', $ToolName,
            '--tool-input-summary', $ToolInputSummary
        )
        $consumeResult = Invoke-HelperJson $args
        [ordered]@{
            ticket = $issued.ticket
            ticket_status = $ticketContext.status
            memory_preflight = $memoryPreflight
            consume = $consumeResult
            wrapper_state_path = $issued.state.state_path
            wrapper_scoped_state_path = $issued.state.state_path
            wrapper_state_index_path = $StateIndexPath
        } | ConvertTo-Json -Depth 8
    }

    'before-mutate' {
        if (-not $ToolInputSummary) {
            throw 'before-mutate requires -ToolInputSummary.'
        }
        $ticketContext = Resolve-TicketContext -CurrentAgentId $AgentId -CurrentBaseUrl $BaseUrl -ExplicitTicketId $TicketId -RequireLiveTicket -RequireWorkspaceBinding
        $args = @(
            '--base-url', $BaseUrl,
            'ticket-consume',
            '--ticket-id', $ticketContext.ticket_id,
            '--agent-id', $AgentId,
            '--tool-name', $ToolName,
            '--tool-input-summary', $ToolInputSummary
        )
        $result = Invoke-HelperJson $args
        if ($ticketContext.status) {
            $result | Add-Member -NotePropertyName ticket_status -NotePropertyValue $ticketContext.status
        }
        if ($ticketContext.state) {
            $result | Add-Member -NotePropertyName wrapper_state_path -NotePropertyValue $ticketContext.state.state_path
            $result | Add-Member -NotePropertyName wrapper_state_selection -NotePropertyValue $ticketContext.state.selection_kind
        }
        $result | ConvertTo-Json -Depth 8
    }

    'after-edit' {
        if (-not $Detail) {
            throw 'after-edit requires -Detail.'
        }
        if ($Action -eq 'done' -and -not $TicketId) {
            throw 'done requires explicit -TicketId; shared wrapper state is never inferred.'
        }
        $autoScopeText = $null
        $autoTargetFiles = @()
        if (-not $TicketId) {
            if (-not $TargetFiles -or $TargetFiles.Count -eq 0) {
                throw 'after-edit refuses to auto-attach a stored ticket without -TicketId or -TargetFiles. Pass the ticket explicitly, or pass the edited target files from prepare so stale cross-task tickets cannot be reused; write one after-edit per prepared scope.'
            }
            $autoScopeText = $Detail
            $autoTargetFiles = $TargetFiles
        }
        if ($Action -eq 'done') {
            # Completion may arrive after lease expiry. The backend validates the
            # durable consumed receipt, so keep the scoped ticket id without
            # requiring the active-ticket GET to succeed.
            $ticketContext = Resolve-TicketContext `
                -CurrentAgentId $AgentId `
                -CurrentBaseUrl $BaseUrl `
                -ExplicitTicketId $TicketId `
                -ExpectedScopeText $autoScopeText `
                -ExpectedTargetFiles $autoTargetFiles `
                -RequireWorkspaceBinding `
                -AllowExpiredExactTicketState
        } else {
            $ticketContext = Resolve-TicketContext `
                -CurrentAgentId $AgentId `
                -CurrentBaseUrl $BaseUrl `
                -ExplicitTicketId $TicketId `
                -ExpectedScopeText $autoScopeText `
                -ExpectedTargetFiles $autoTargetFiles `
                -RequireLiveTicket `
                -RequireWorkspaceBinding `
                -AllowInvalidCachedTicket
        }
        $resolvedTicketId = $ticketContext.ticket_id
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
        if ($ticketContext.status) {
            $result | Add-Member -NotePropertyName ticket_status -NotePropertyValue $ticketContext.status
        }
        if ($ticketContext.state) {
            $result | Add-Member -NotePropertyName wrapper_state_selection -NotePropertyValue $ticketContext.state.selection_kind
        }
        if ($ticketContext.invalid_cached_ticket_cleared) {
            $result | Add-Member -NotePropertyName invalid_cached_ticket_cleared -NotePropertyValue $true
        }
        $result | ConvertTo-Json -Depth 6
    }

    'before-compact' {
        if (-not $TaskSummary) {
            throw 'before-compact requires -TaskSummary.'
        }
        $ticketContext = $null
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
        if ($TicketId) {
            $ticketContext = Resolve-TicketContext `
                -CurrentAgentId $AgentId `
                -CurrentBaseUrl $BaseUrl `
                -ExplicitTicketId $TicketId `
                -ExpectedScopeText $TaskSummary `
                -ExpectedTargetFiles $TargetFiles `
                -RequireLiveTicket `
                -RequireWorkspaceBinding
            $ticketScope = $ticketContext.status.ticket.scope
            if ($ticketScope -and $ticketScope.target_files) {
                foreach ($item in $ticketScope.target_files) {
                    if ($item -and ($compactFiles -notcontains $item)) {
                        $compactFiles += $item
                    }
                }
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
        if ($ticketContext -and $ticketContext.ticket_id) {
            $result | Add-Member -NotePropertyName ticket_id_context -NotePropertyValue $ticketContext.ticket_id
        }
        if ($ticketContext -and $ticketContext.status) {
            $result | Add-Member -NotePropertyName ticket_status -NotePropertyValue $ticketContext.status
        }
        $result | Add-Member -NotePropertyName compact_scope_files -NotePropertyValue $compactFiles
        if (-not $TicketId) {
            $result | Add-Member -NotePropertyName compact_scope_selection -NotePropertyValue 'explicit_files_only'
        } else {
            $result | Add-Member -NotePropertyName compact_scope_selection -NotePropertyValue 'explicit_live_ticket'
        }
        $result | ConvertTo-Json -Depth 8
    }

    'check-ticket' {
        $ticketContext = Resolve-TicketContext -CurrentAgentId $AgentId -CurrentBaseUrl $BaseUrl -ExplicitTicketId $TicketId -RequireLiveTicket -RequireWorkspaceBinding
        [ordered]@{
            ticket_id = $ticketContext.ticket_id
            state = $ticketContext.state
            status = $ticketContext.status
        } | ConvertTo-Json -Depth 8
    }

    'show-state' {
        $state = Load-State -CurrentAgentId $AgentId -CurrentBaseUrl $BaseUrl -ExpectedTargetFiles $TargetFiles
        if (-not $state) {
            Write-Output '{"state":"missing"}'
        } else {
            $ticketContext = Resolve-TicketContext -CurrentAgentId $AgentId -CurrentBaseUrl $BaseUrl -ExplicitTicketId $TicketId
            [ordered]@{
                state = $state
                ticket_status = $ticketContext.status
            } | ConvertTo-Json -Depth 8
        }
    }

    'clear-state' {
        if (Test-Path $AgentWorkspaceStateDir) {
            Remove-Item -LiteralPath $AgentWorkspaceStateDir -Recurse -Force
        }
        Remove-Item Env:THREECAN_TICKET_ID -ErrorAction SilentlyContinue
        Write-Output '{"cleared":true}'
    }
}
