@echo off
REM 维护：环境自检 / 统计 / 备份。
REM 不带参数 = 自检。用法：3-maintain.cmd [check^|stats^|backup^|reindex-fst^|vacuum]
setlocal
set "ROOT=%~dp0.."
set "PY=%ROOT%\.venv\Scripts\python.exe"
set "PYTHONUTF8=1"
set "HF_ENDPOINT=https://hf-mirror.com"
set "HF_HUB_DISABLE_SYMLINKS_WARNING=1"

if not exist "%PY%" (
  echo [XX] Python venv not found: %PY%
  exit /b 1
)

if "%~1"=="" (
  "%PY%" -X utf8 "%ROOT%\offline\maintain.py" check
) else (
  "%PY%" -X utf8 "%ROOT%\offline\maintain.py" %*
)
set "RC=%ERRORLEVEL%"
echo.
pause
exit /b %RC%
