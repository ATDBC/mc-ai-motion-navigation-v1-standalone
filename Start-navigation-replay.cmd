@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\navigation_replay\start-replay.ps1"
if errorlevel 1 pause
endlocal
