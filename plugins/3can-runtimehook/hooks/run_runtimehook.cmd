@echo off
setlocal

if /i "%~1"=="-SessionOrientation" goto run_hook

set "runtimehook_cursor=%CD%"
:find_repo
if exist "%runtimehook_cursor%\.git" goto check_state
for %%I in ("%runtimehook_cursor%\..") do set "runtimehook_parent=%%~fI"
if /i "%runtimehook_parent%"=="%runtimehook_cursor%" goto inactive
set "runtimehook_cursor=%runtimehook_parent%"
goto find_repo

:check_state
if exist "%runtimehook_cursor%\.codex\runtimehook" goto run_hook

:inactive
"%SystemRoot%\System32\more.com" >nul
exit /b 0

:run_hook
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -File "%~dp0run_runtimehook.ps1" %*
exit /b %ERRORLEVEL%
