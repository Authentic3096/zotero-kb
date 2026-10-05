@echo off
REM ============================================================
REM  Optional: install MinerU (better PDF parsing) for the
REM  knowledge base.  Double-click this file.
REM
REM  It will:
REM    1. download uv (single file, no system changes)
REM    2. create .mineru\.venv (Python 3.12; uv downloads it if needed)
REM    3. install mineru[torch] + swap torch for the CUDA build
REM    4. download models (basic tier ~0.9 GB)
REM
REM  All ASCII + PowerShell only (avoids cmd choking on UTF-8 in
REM  the GBK code page).  Extra flags are forwarded, e.g.:
REM    install-mineru.cmd -WithVlm
REM    install-mineru.cmd -Force
REM ============================================================
setlocal
set "PS1=%~dp0install-mineru.ps1"

if not exist "%PS1%" (
  echo [XX] Missing: %PS1%
  exit /b 1
)

REM -ExecutionPolicy Bypass: do not touch the user's execution policy
powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%" %*
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
  echo [OK] Done.
) else if "%RC%"=="2" (
  echo [OK] Done, but CUDA is unavailable ^(will run on CPU^).
) else (
  echo [XX] Failed with exit code %RC%
)
pause
exit /b %RC%
