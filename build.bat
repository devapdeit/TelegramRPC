@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul 2>&1

echo =====================================================
echo   Telegram RPC ^| By Apdeit - Windows EXE builder
echo =====================================================
echo.

set "PY_CMD="
py -3.14 -c "import sys" >nul 2>&1 && set "PY_CMD=py -3.14"
if not defined PY_CMD py -3.13 -c "import sys" >nul 2>&1 && set "PY_CMD=py -3.13"
if not defined PY_CMD py -3.12 -c "import sys" >nul 2>&1 && set "PY_CMD=py -3.12"
if not defined PY_CMD py -3.11 -c "import sys" >nul 2>&1 && set "PY_CMD=py -3.11"
if not defined PY_CMD python -c "import sys" >nul 2>&1 && set "PY_CMD=python"

if not defined PY_CMD goto no_python

%PY_CMD% prepare_build.py
if errorlevel 1 goto config_error

if not exist ".buildvenv\Scripts\python.exe" (
    echo [1/4] Creating build environment...
    %PY_CMD% -m venv .buildvenv
    if errorlevel 1 goto build_error
)

echo [2/4] Installing build packages...
".buildvenv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto build_error
".buildvenv\Scripts\python.exe" -m pip install -r requirements-build.txt
if errorlevel 1 goto build_error

echo [3/4] Building one-file Windows application...
".buildvenv\Scripts\python.exe" -m PyInstaller --noconfirm --clean TelegramRPC.spec
if errorlevel 1 goto build_error

echo [4/4] Build completed.
echo.
echo EXE file:
echo %CD%\dist\TelegramRPC_By_Apdeit.exe
echo.
echo Share ONLY this EXE file with your friends.
pause
exit /b 0

:no_python
echo Python was not found. Install Python 3.11-3.14 and enable Add Python to PATH.
pause
exit /b 1

:config_error
echo.
echo Open build_config.json and insert your numeric Discord Application ID.
pause
exit /b 1

:build_error
echo.
echo Build failed. Copy the error text or send a screenshot.
pause
exit /b 1
