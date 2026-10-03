@echo off
REM ============================================================
REM  一键装好知识库的 Python 环境（给别人用的时候跑这个）
REM
REM  它会做四件事：
REM    1. 下载 uv（单文件，免安装，不动系统）
REM    2. 用它建 .venv —— 本机没 Python 时 uv 会自己下载一个
REM    3. 装依赖（走阿里云镜像）
REM    4. 下载嵌入模型（约 90 MB，走 hf-mirror.com）
REM
REM  全部 ASCII + 只调用 PowerShell，避免 cmd 在 GBK 代码页下解析中文出错。
REM  Python 版本想换：install-env.cmd 3.11
REM ============================================================
setlocal
set "PS1=%~dp0install-env.ps1"

if not exist "%PS1%" (
  echo [XX] Missing: %PS1%
  exit /b 1
)

REM -ExecutionPolicy Bypass：不改用户的执行策略（默认 Restricted 会拦住脚本）
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
