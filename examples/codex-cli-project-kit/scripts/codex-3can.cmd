@echo off
setlocal
set "CHCP_EXE=%SystemRoot%\System32\chcp.com"
if not exist "%CHCP_EXE%" set "CHCP_EXE=C:\Windows\System32\chcp.com"
if not exist "%CHCP_EXE%" set "CHCP_EXE=chcp"
for /f "tokens=2 delims=:" %%G in ('"%CHCP_EXE%"') do set "CODEPAGE=%%G"
set "CODEPAGE=%CODEPAGE: =%"
"%CHCP_EXE%" 65001 >nul
set "SCRIPT_DIR=%~dp0"
pushd "%SCRIPT_DIR%.." || (
    if defined CODEPAGE "%CHCP_EXE%" %CODEPAGE% >nul
    exit /b 1
)
C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\codex-3can.ps1" %*
set "EXIT_CODE=%ERRORLEVEL%"
popd
if defined CODEPAGE "%CHCP_EXE%" %CODEPAGE% >nul
exit /b %EXIT_CODE%
