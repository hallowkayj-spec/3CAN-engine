$ErrorActionPreference = "Stop"
$env:NoDefaultCurrentDirectoryInExePath = "1"

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
        exit 0
    }

    $python = $null
    foreach ($rawDirectory in ($env:PATH -split ';')) {
        $expanded = [Environment]::ExpandEnvironmentVariables($rawDirectory.Trim().Trim('"'))
        if ([string]::IsNullOrWhiteSpace($expanded) -or -not [IO.Path]::IsPathRooted($expanded)) {
            continue
        }
        foreach ($name in @("python.exe", "python3.exe")) {
            $candidate = [IO.Path]::GetFullPath((Join-Path $expanded $name))
            if (
                (Test-Path -LiteralPath $candidate -PathType Leaf) -and
                -not (Test-Within -Path $candidate -Root $untrustedRoot)
            ) {
                $python = $candidate
                break
            }
        }
        if ($null -ne $python) {
            break
        }
    }
    if ($null -eq $python) {
        exit 0
    }

    & $python $controller hook --session-orientation
    exit $LASTEXITCODE
}
catch {
    exit 0
}
