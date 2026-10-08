@echo off
setlocal
cd /d "%~dp0"
python service.py start
if errorlevel 1 (
  pause
  exit /b 1
)
start "" "http://127.0.0.1:8766/"
endlocal
