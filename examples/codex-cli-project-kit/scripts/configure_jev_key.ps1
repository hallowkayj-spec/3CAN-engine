# Run interactively on Windows. Never paste a key into a chat, command argument or source file.
param([string]$CodexHomePath = $env:CODEX_HOME)
$ErrorActionPreference = 'Stop'
$taskCodexHome = if ($CodexHomePath) { $CodexHomePath } else { Join-Path $env:USERPROFILE '.codex' }
if (-not [IO.Path]::IsPathRooted($taskCodexHome)) { throw 'CODEX_HOME must be absolute.' }
$credentialDirectory = Join-Path $taskCodexHome 'credentials'
$credentialPath = Join-Path $credentialDirectory 'runtimehook-openrouter.clixml'
if (Test-Path -LiteralPath $credentialPath) { throw 'Credential already exists. Remove only this credential after revoking the old key if rotation is intended.' }
$key = Read-Host 'Paste OpenRouter API key (hidden; never send it in chat)' -AsSecureString
if ($key.Length -eq 0) { throw 'Empty key; nothing saved.' }
try {
    New-Item -ItemType Directory -Path $credentialDirectory -Force | Out-Null
    # Export-Clixml encrypts SecureString with Windows DPAPI for the current user/machine.
    $key | Export-Clixml -LiteralPath $credentialPath
    Write-Host 'Saved using Windows DPAPI. No Codex restart needed. Verify with a small sanitized assessment.'
} finally {
    $key.Dispose()
}
