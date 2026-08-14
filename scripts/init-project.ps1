param(
    [string]$ProjectDir = (Get-Location).Path,
    [int]$Port = 9711,
    [int]$MinNodes = 10,
    [switch]$ApplyProjectSeeds,
    [switch]$StartServer,
    [switch]$NoSeed
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ReleaseRoot = Split-Path -Parent $ScriptDir
$EngineRoot = Join-Path $ReleaseRoot "neural-memory"
$GraphDir = Join-Path $EngineRoot "graph"
$BaseUrl = "http://127.0.0.1:$Port"
$ProjectDir = (Resolve-Path $ProjectDir).Path

function Write-Utf8NoBom {
    param([string]$Path, [string]$Text)
    $encoding = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::WriteAllText($Path, $Text, $encoding)
}

function Invoke-PythonChecked {
    param([string[]]$Arguments)
    & python @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "python failed: $($Arguments -join ' ')"
    }
}

function Get-RuntimePathSha256 {
    param([string]$Path)
    $canonical = (Resolve-Path -LiteralPath $Path).Path.ToLowerInvariant()
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($canonical)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        return ([System.BitConverter]::ToString($sha.ComputeHash($bytes))).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $sha.Dispose()
    }
}

function Get-BoundSidecar {
    param(
        [string]$ExpectedPython,
        [string]$ExpectedEngineSha256,
        [string]$ExpectedGraphSha256
    )
    $listeners = @(
        Get-NetTCPConnection -State Listen -ErrorAction Stop |
            Where-Object { $_.LocalAddress -eq "127.0.0.1" -and $_.LocalPort -eq $Port }
    )
    if ($listeners.Count -eq 0) {
        return $null
    }
    if ($listeners.Count -ne 1) {
        throw "THREECAN_SIDECAR_LISTENER_AMBIGUOUS"
    }

    $listenerPid = [int]$listeners[0].OwningProcess
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$listenerPid" -ErrorAction Stop
    if (-not $process -or
        -not [string]::Equals($process.ExecutablePath, $ExpectedPython, [System.StringComparison]::OrdinalIgnoreCase) -or
        $process.CommandLine -notmatch "backend[\\/]app\.py" -or
        $process.CommandLine -notmatch "--port\s+$Port(?:\s|$)") {
        throw "THREECAN_SIDECAR_PROCESS_IDENTITY_MISMATCH"
    }

    $stats = Invoke-RestMethod -Uri "$BaseUrl/api/stats?deep=true" -TimeoutSec 20
    if (-not $stats.readiness.development_ready -or
        $stats.runtime_identity.engine_root_sha256 -ne $ExpectedEngineSha256 -or
        $stats.runtime_identity.graph_root_sha256 -ne $ExpectedGraphSha256) {
        throw "THREECAN_SIDECAR_RUNTIME_IDENTITY_MISMATCH"
    }

    return [pscustomobject]@{
        process = $process
        stats = $stats
    }
}

function Write-SidecarReceipt {
    param(
        [string]$Path,
        [object]$Bound,
        [string]$Source
    )
    $payload = [ordered]@{
        schema_version = "3can.project-sidecar-owner/v1"
        status = "READY"
        observed_at_utc = [DateTime]::UtcNow.ToString("o")
        source = $Source
        base_url = $BaseUrl
        pid = [int]$Bound.process.ProcessId
        creation_date = $Bound.process.CreationDate.ToString("o")
        executable = [string]$Bound.process.ExecutablePath
        command_line = [string]$Bound.process.CommandLine
        engine_root_sha256 = [string]$Bound.stats.runtime_identity.engine_root_sha256
        graph_root_sha256 = [string]$Bound.stats.runtime_identity.graph_root_sha256
        development_ready = [bool]$Bound.stats.readiness.development_ready
        production_ready = [bool]$Bound.stats.readiness.production_ready
    }
    $transactionId = [Guid]::NewGuid().ToString("N")
    $temp = "$Path.$transactionId.tmp"
    [System.IO.File]::WriteAllText(
        $temp,
        ($payload | ConvertTo-Json -Depth 5),
        [System.Text.UTF8Encoding]::new($false)
    )
    if (Test-Path -LiteralPath $Path) {
        $backup = "$Path.$transactionId.bak"
        [System.IO.File]::Replace($temp, $Path, $backup)
        Remove-Item -LiteralPath $backup -Force -ErrorAction SilentlyContinue
    }
    else {
        [System.IO.File]::Move($temp, $Path)
    }
}

New-Item -ItemType Directory -Force -Path (Join-Path $GraphDir "nodes") | Out-Null
foreach ($name in @("edges.json", "agents.json", "activity_log.json")) {
    $path = Join-Path $GraphDir $name
    if (-not (Test-Path $path)) {
        Write-Utf8NoBom -Path $path -Text "[]`n"
    }
}

$env:THREECAN_ENGINE_ROOT = $EngineRoot
$env:THREECAN_GRAPH_DIR = $GraphDir
$env:THREECAN_PROJECT_DIR = $ProjectDir
$env:THREECAN_BASE_URL = $BaseUrl
$env:THREECAN_MIN_NODES = [string]$MinNodes
$env:THREECAN_READINESS_MODE = "development"

Push-Location $EngineRoot
try {
    if (-not $NoSeed) {
        Invoke-PythonChecked @("backend\seed_nodes.py")
    }
    if ($ApplyProjectSeeds) {
        Invoke-PythonChecked @("tools\project_bootstrapper.py", "--project", $ProjectDir, "--base-url", $BaseUrl, "--apply")
    } else {
        Invoke-PythonChecked @("tools\project_bootstrapper.py", "--project", $ProjectDir, "--base-url", $BaseUrl, "--dry-run")
    }
}
finally {
    Pop-Location
}

if ($StartServer) {
    $LogDir = Join-Path $EngineRoot "logs"
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
    $lockPath = Join-Path $LogDir "3can_$Port.start.lock"
    try {
        $lock = [System.IO.File]::Open($lockPath, "OpenOrCreate", "ReadWrite", "None")
    }
    catch [System.IO.IOException] {
        throw "THREECAN_SIDECAR_START_BUSY"
    }
    try {
        $pythonExe = (Get-Command python -CommandType Application -ErrorAction Stop).Source
        $expectedEngineSha256 = Get-RuntimePathSha256 $EngineRoot
        $expectedGraphSha256 = Get-RuntimePathSha256 $GraphDir
        $receiptPath = Join-Path $LogDir "3can_$Port.sidecar-owner.json"
        $bound = Get-BoundSidecar $pythonExe $expectedEngineSha256 $expectedGraphSha256
        if ($bound) {
            Write-SidecarReceipt $receiptPath $bound "already_running"
            Write-Host "[3CAN] already ready $BaseUrl"
        }
        else {
            $runId = [Guid]::NewGuid().ToString("N")
            $stdout = Join-Path $LogDir "3can_$Port.$runId.stdout.log"
            $stderr = Join-Path $LogDir "3can_$Port.$runId.stderr.log"
            $appPath = Join-Path $EngineRoot "backend\app.py"
            $child = Start-Process $pythonExe -WindowStyle Hidden -WorkingDirectory $EngineRoot -PassThru `
                -ArgumentList "-B", "`"$appPath`"", "--host", "127.0.0.1", "--port", "$Port" `
                -RedirectStandardOutput $stdout -RedirectStandardError $stderr
            $deadline = [DateTime]::UtcNow.AddSeconds(60)
            do {
                Start-Sleep -Milliseconds 250
                try {
                    $bound = Get-BoundSidecar $pythonExe $expectedEngineSha256 $expectedGraphSha256
                }
                catch {
                    if ([DateTime]::UtcNow -ge $deadline) {
                        throw
                    }
                }
            } while (-not $bound -and [DateTime]::UtcNow -lt $deadline -and -not $child.HasExited)

            if (-not $bound) {
                throw "THREECAN_SIDECAR_START_FAILED"
            }
            Write-SidecarReceipt $receiptPath $bound $(if ($bound.process.ProcessId -eq $child.Id) { "started" } else { "concurrent_owner_won" })
            Write-Host "[3CAN] ready $BaseUrl (PID $($bound.process.ProcessId))"
        }
    }
    catch {
        if ($child -and -not $child.HasExited) {
            $current = Get-Process -Id $child.Id -ErrorAction SilentlyContinue
            if ($current -and $current.StartTime.ToUniversalTime() -eq $child.StartTime.ToUniversalTime()) {
                Stop-Process -Id $child.Id -Force
            }
        }
        throw
    }
    finally {
        if ($lock) {
            $lock.Dispose()
        }
    }
}

Write-Host "[3CAN] project initialized"
Write-Host "  engine:  $EngineRoot"
Write-Host "  graph:   $GraphDir"
Write-Host "  project: $ProjectDir"
Write-Host "  base:    $BaseUrl"
Write-Host "  token:   $BaseUrl/static/token_usage.html"
