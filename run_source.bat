@echo off
setlocal
cd /d "%~dp0"
if not exist ".buildvenv\Scripts\python.exe" (
    echo Run build.bat once to create the environment.
    pause
    exit /b 1
)
".buildvenv\Scripts\python.exe" main.py
