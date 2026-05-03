@echo off
chcp 65001 > nul
cd /d "%~dp0"

set "VENV_DIR=.venv"
set "VENV_PYTHON=%VENV_DIR%\Scripts\python.exe"

echo ==========================================
echo Auto Image Viewer install
echo ==========================================

python --version >nul 2>&1
if %errorlevel% neq 0 (
    color 4F
    echo [ERROR] Python was not found. Install Python first and enable "Add python.exe to PATH".
    pause
    exit /b 1
)

if not exist requirements.txt (
    color 4F
    echo [ERROR] requirements.txt was not found.
    pause
    exit /b 1
)

if not exist "%VENV_PYTHON%" (
    echo Creating local virtual environment...
    python -m venv "%VENV_DIR%"
    if %errorlevel% neq 0 (
        color 4F
        echo [ERROR] Failed to create %VENV_DIR%.
        pause
        exit /b 1
    )
)

echo Installing required packages into %VENV_DIR%...
"%VENV_PYTHON%" -m pip install --upgrade pip
if %errorlevel% neq 0 (
    color 4F
    echo [ERROR] Failed to upgrade pip in %VENV_DIR%.
    pause
    exit /b 1
)

"%VENV_PYTHON%" -m pip install -r requirements.txt
if %errorlevel% neq 0 (
    color 4F
    echo [ERROR] Failed to install required packages into %VENV_DIR%.
    pause
    exit /b 1
)

echo.
echo Install completed. Virtual environment: %VENV_DIR%
pause
