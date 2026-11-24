@echo off
cd /d "%~dp0"
py -3 app.py start
if errorlevel 1 (
  echo.
  echo SCOPE could not start. Check that Python 3.10 or later is installed.
  echo The project guide explains other errors and how to use a different port.
  pause
)
