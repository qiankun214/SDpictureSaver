@echo off
chcp 65001 >nul
setlocal
rem pictureSaver2 - start the DESKTOP app (QtWebEngine window showing the same web UI)
rem usage: run_desktop.cmd [port]     default 8765 (falls back to a free port if busy)

set "ROOT=%~dp0"
set "PORT=%~1"
if "%PORT%"=="" set "PORT=8765"

set "PYTHONPATH=%ROOT%src"
set "PYTHONIOENCODING=utf-8"

echo.
echo   pictureSaver2 desktop - opening the native window (port %PORT%)
echo   (close the window to quit)
echo.

python -m picsaver desktop --port %PORT%
if errorlevel 1 (
  echo.
  echo   Failed to start. Check that Python 3.10+ and PySide6 are available.
  echo   Log: %LOCALAPPDATA%\pictureSaver2\desktop.log
  pause
)
endlocal
