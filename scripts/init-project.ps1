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
    $stdout = Join-Path $LogDir "3can_$Port.stdout.log"
    $stderr = Join-Path $LogDir "3can_$Port.stderr.log"
    $cmd = @"
`$env:THREECAN_ENGINE_ROOT='$EngineRoot'
`$env:THREECAN_GRAPH_DIR='$GraphDir'
`$env:THREECAN_PROJECT_DIR='$ProjectDir'
`$env:THREECAN_BASE_URL='$BaseUrl'
`$env:THREECAN_MIN_NODES='$MinNodes'
`$env:THREECAN_READINESS_MODE='development'
Set-Location '$EngineRoot'
python backend\app.py --port $Port
"@
    $ShellExe = (Get-Process -Id $PID).Path
    Start-Process $ShellExe -WindowStyle Hidden -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", $cmd -RedirectStandardOutput $stdout -RedirectStandardError $stderr
    Write-Host "[3CAN] started $BaseUrl"
}

Write-Host "[3CAN] project initialized"
Write-Host "  engine:  $EngineRoot"
Write-Host "  graph:   $GraphDir"
Write-Host "  project: $ProjectDir"
Write-Host "  base:    $BaseUrl"
Write-Host "  token:   $BaseUrl/static/token_usage.html"
