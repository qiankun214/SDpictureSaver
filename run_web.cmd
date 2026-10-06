@echo off
chcp 65001 >nul
setlocal
rem pictureSaver2 - start the web UI (no PowerShell execution policy involved)
rem usage: run_web.cmd [port]      default 8765

set "ROOT=%~dp0"
set "PORT=%~1"
if "%PORT%"=="" set "PORT=8765"

set "PYTHONPATH=%ROOT%src"
set "PYTHONIOENCODING=utf-8"

echo.
echo   pictureSaver2  -  Web UI at  http://127.0.0.1:%PORT%/
echo   (press Ctrl+C to stop)
echo.

python -m picsaver web --host 127.0.0.1 --port %PORT%
if errorlevel 1 (
  echo.
  echo   Failed to start. Check that Python 3.10+ is on PATH.
  pause
)
endlocal
