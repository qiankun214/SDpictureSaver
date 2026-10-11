@echo off
chcp 65001 >nul
setlocal
rem pictureSaver2 - start the DESKTOP app (QtWebEngine window showing the same web UI)
rem
rem usage:
rem   run_desktop.cmd                     default port 8765, opens the window
rem   run_desktop.cmd 9000                use 9000 as the preferred port
rem   run_desktop.cmd --smoke             self-check only (no window), for troubleshooting
rem   run_desktop.cmd 9000 --cdp-port 9222
rem
rem Anything that does not start with "-" is treated as the preferred port;
rem everything else is forwarded to `python -m picsaver desktop`.

set "ROOT=%~dp0"
set "PORT=8765"
set "EXTRA="

:parse
if "%~1"=="" goto run
set "ARG=%~1"
if "%ARG:~0,1%"=="-" (
  set "EXTRA=%EXTRA% %ARG%"
) else (
  set "PORT=%ARG%"
)
shift
goto parse

:run
set "PYTHONPATH=%ROOT%src"
set "PYTHONIOENCODING=utf-8"

echo.
echo   pictureSaver2 desktop - native window (preferred port %PORT%)
echo   close the window to quit; log: %LOCALAPPDATA%\pictureSaver2\desktop.log
echo.

python -m picsaver desktop --port %PORT% %EXTRA%
if errorlevel 1 (
  echo.
  echo   Failed to start. Check that Python 3.10+ and PySide6 are available.
  echo   Log: %LOCALAPPDATA%\pictureSaver2\desktop.log
  pause
)
endlocal
