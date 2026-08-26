param([switch]$SessionOrientation)

$ErrorActionPreference = "Stop"
$env:NoDefaultCurrentDirectoryInExePath = "1"
$utf8NoBom = New-Object Text.UTF8Encoding($false)
[Console]::InputEncoding = $utf8NoBom
$OutputEncoding = $utf8NoBom

function Write-RuntimeHookUnavailable {
    param([string]$Reason)

    @{
        systemMessage = (
            "RuntimeHook semantic context is UNAVAILABLE: $Reason. " +
            "Safe local work may continue; independent project evidence gates remain authoritative."
        )
    } | ConvertTo-Json -Compress
}

function Test-Within {
    param([string]$Path, [string]$Root)

    $comparison = [StringComparison]::OrdinalIgnoreCase
    $separator = [IO.Path]::DirectorySeparatorChar
    $rootPrefix = $Root.TrimEnd([char[]]@('\', '/')) + $separator
    return $Path.Equals($Root, $comparison) -or $Path.StartsWith($rootPrefix, $comparison)
}

try {
    $current = [IO.Path]::GetFullPath((Get-Location).Path)
    $boundary = $null
    $cursor = Get-Item -LiteralPath $current
    while ($null -ne $cursor) {
        if (Test-Path -LiteralPath (Join-Path $cursor.FullName ".git")) {
            $boundary = $cursor.FullName
            break
        }
        $cursor = $cursor.Parent
    }
    $untrustedRoot = if ($null -ne $boundary) { $boundary } else { $current }

    if (-not $SessionOrientation) {
        $statePath = if ($null -ne $boundary) {
            Join-Path $boundary ".codex\runtimehook\state.json"
        }
        else {
            $null
        }
        $stateEntry = if ($null -ne $statePath) {
            Get-Item -Force -LiteralPath $statePath -ErrorAction SilentlyContinue
        }
        else {
            $null
        }
        if ($null -eq $stateEntry) {
            [Console]::In.ReadToEnd() | Out-Null
            exit 0
        }
    }
    $hookInput = [Console]::In.ReadToEnd()

    $controller = Join-Path $env:PLUGIN_ROOT "skills\3can-runtimehook\scripts\3can_runtimehook.py"
    if (-not (Test-Path -LiteralPath $controller -PathType Leaf)) {
        Write-RuntimeHookUnavailable "the bundled controller is missing"
        exit 0
    }

    $python = $null
    foreach ($rawDirectory in ($env:PATH -split ';')) {
        $expanded = [Environment]::ExpandEnvironmentVariables($rawDirectory.Trim().Trim('"'))
        if ([string]::IsNullOrWhiteSpace($expanded) -or -not [IO.Path]::IsPathRooted($expanded)) {
            continue
        }
        foreach ($name in @("python.exe", "python3.exe", "py.exe")) {
            $candidate = [IO.Path]::GetFullPath((Join-Path $expanded $name))
            if (
                (Test-Path -LiteralPath $candidate -PathType Leaf) -and
                -not (Test-Within -Path $candidate -Root $untrustedRoot)
            ) {
                $probeArguments = @(
                    "-c",
                    "import pathlib, sys; print(pathlib.Path(sys.executable).resolve() if sys.version_info.major == 3 else '')"
                )
                if ($name -eq "py.exe") {
                    $probeArguments = @("-3") + $probeArguments
                }
                try {
                    $resolved = & $candidate @probeArguments 2>$null
                    if ($LASTEXITCODE -eq 0 -and $null -ne $resolved) {
                        $resolved = ($resolved | Select-Object -Last 1).Trim()
                        if (-not [string]::IsNullOrWhiteSpace($resolved)) {
                            $resolved = [IO.Path]::GetFullPath($resolved)
                            if (
                                (Test-Path -LiteralPath $resolved -PathType Leaf) -and
                                -not (Test-Within -Path $resolved -Root $untrustedRoot)
                            ) {
                                $python = $resolved
                            }
                        }
                    }
                }
                catch {
                    $python = $null
                }
                if ($null -ne $python) {
                    break
                }
            }
        }
        if ($null -ne $python) {
            break
        }
    }
    if ($null -eq $python) {
        Write-RuntimeHookUnavailable "Python 3 is not available on PATH"
        exit 0
    }

    $controllerArguments = @($controller, "hook")
    if ($SessionOrientation) {
        $controllerArguments += "--session-orientation"
    }
    $controllerOutput = $hookInput | & $python @controllerArguments
    $controllerExit = $LASTEXITCODE
    if ($controllerExit -ne 0) {
        Write-RuntimeHookUnavailable "Python 3 could not execute the bundled controller"
    }
    else {
        $controllerOutput
    }
    exit 0
}
catch {
    Write-RuntimeHookUnavailable "the Windows launcher failed"
    exit 0
}
