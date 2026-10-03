@echo off
REM Sideload the Zotero plugin (bypasses the UI install compatibility check).
REM Pure ASCII only -- Chinese text in a .cmd gets mangled by cmd.exe codepages.

setlocal
cd /d "%~dp0.."
set "PY=%~dp0..\.venv\Scripts\python.exe"
set "TOOL=%~dp0..\tools\sideload_plugin.py"

echo ============================================================
echo  Sideload Zotero plugin
echo ============================================================
echo.
echo  IMPORTANT: fully QUIT Zotero first (not just minimize).
echo  Otherwise Zotero writes its config back on exit and the
echo  sideload will not take effect.
echo.

if not exist "%PY%" (
    echo [XX] Python not found: %PY%
    echo      Build the venv first ^(see README^).
    goto :end
)
if not exist "%TOOL%" (
    echo [XX] Tool not found: %TOOL%
    goto :end
)

"%PY%" -X utf8 "%TOOL%" install
echo.
echo Exit code: %ERRORLEVEL%

:end
echo.
pause
