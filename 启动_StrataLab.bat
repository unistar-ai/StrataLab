@echo off
rem Strata Lab: one console-less pythonw process that raises its own app window at 8765.
setlocal
cd /d "%~dp0"
set "PYW=%~dp0..\Strata-data\terminal\current\python\pythonw.exe"
set "APP=%~dp0app.py"
if not exist "%PYW%" set "PYW=pythonw"
if not exist "%APP%" (
  echo [ERROR] app.py not found: %APP%
  pause
  exit /b 1
)
start "" "%PYW%" "%APP%"
exit /b 0