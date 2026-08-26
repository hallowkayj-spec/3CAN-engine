$ErrorActionPreference = "Stop"
$env:NoDefaultCurrentDirectoryInExePath = "1"

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
        }
        $cursor = $cursor.Parent
    }
    $untrustedRoot = if ($null -ne $boundary) { $boundary } else { $current }

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
                if ($name -eq "py.exe") {
                    $resolved = & $candidate -3 -c "import sys; print(sys.executable)"
                    if ($LASTEXITCODE -eq 0 -and $null -ne $resolved) {
                        $resolved = [IO.Path]::GetFullPath(($resolved | Select-Object -Last 1).Trim())
                        if (
                            (Test-Path -LiteralPath $resolved -PathType Leaf) -and
                            -not (Test-Within -Path $resolved -Root $untrustedRoot)
                        ) {
                            $python = $resolved
                        }
                    }
                }
                else {
                    $python = $candidate
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

    $controllerOutput = & $python $controller hook --session-orientation
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
