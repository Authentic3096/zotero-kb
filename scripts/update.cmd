@echo off
REM ============================================================
REM  Update a portable install: copy program files, keep user data.
REM  Run this from the NEW extracted folder; it asks for the old one.
REM
REM  All ASCII, PowerShell does the real work.
REM ============================================================
setlocal
set "PS1=%~dp0update.ps1"

if not exist "%PS1%" (
  echo [XX] Missing: %PS1%
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%" %*
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
  echo [OK] Done.
) else (
  echo [XX] Failed with exit code %RC%
)
pause
exit /b %RC%
