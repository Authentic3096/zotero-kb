@echo off
REM 离线管道：把 Zotero 库转成知识库（增量）。
REM 新导入了文献、改了笔记/标注之后跑这个。
REM 全部 ASCII，避免 cmd 在 GBK 代码页下解析中文出错。
setlocal
set "ROOT=%~dp0.."
set "PY=%ROOT%\.venv\Scripts\python.exe"
set "PYTHONUTF8=1"
set "HF_ENDPOINT=https://hf-mirror.com"
set "HF_HUB_DISABLE_SYMLINKS_WARNING=1"

if not exist "%PY%" (
  echo [XX] Python venv not found: %PY%
  echo      See README.md section "Install".
  exit /b 1
)

echo ============================================================
echo  Building knowledge base from Zotero (incremental)
echo ============================================================
"%PY%" -X utf8 "%ROOT%\offline\convert.py" %*
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (
  echo [OK] Done.
) else (
  echo [XX] Failed with exit code %RC%
)
pause
exit /b %RC%
