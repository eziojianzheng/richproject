@echo off
setlocal
cd /d "%~dp0"
echo Starting Playwright monitor debugger (debug.enabled must be true)...
py tools\debug\monitor_debug.py --headed %*
set EXIT_CODE=%ERRORLEVEL%
echo.
echo Debugger exit code: %EXIT_CODE%
pause
exit /b %EXIT_CODE%
