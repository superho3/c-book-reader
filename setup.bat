@echo off
REM ════════════════════════════════════════════════════════════════════════════
REM  C-BOOK ECU Code Analyzer — Auto Setup Script for Colleagues
REM ════════════════════════════════════════════════════════════════════════════

setlocal enabledelayedexpansion

echo.
echo ╔════════════════════════════════════════════════════════════════╗
echo ║  C-BOOK ECU Code Analyzer - Setup                              ║
echo ╚════════════════════════════════════════════════════════════════╝
echo.

REM Check Python is installed
python --version >nul 2>&1
if errorlevel 1 (
    echo ❌ ERROR: Python not found. Please install Python 3.10+ and add to PATH.
    pause
    exit /b 1
)

echo ✓ Python detected: 
python --version
echo.

REM Create virtual environment
echo [1/4] Creating Python virtual environment...
if exist .venv (
    echo     ✓ Virtual environment already exists, skipping...
) else (
    python -m venv .venv
    if errorlevel 1 (
        echo ❌ Failed to create virtual environment
        pause
        exit /b 1
    )
    echo ✓ Virtual environment created
)
echo.

REM Activate virtual environment
echo [2/4] Activating virtual environment...
call .\.venv\Scripts\activate.bat
if errorlevel 1 (
    echo ❌ Failed to activate virtual environment
    pause
    exit /b 1
)
echo ✓ Virtual environment activated
echo.

REM Install dependencies
echo [3/4] Installing Python dependencies from requirements.txt...
pip install -q -r requirements.txt
if errorlevel 1 (
    echo ❌ Failed to install dependencies
    pause
    exit /b 1
)
echo ✓ Dependencies installed
echo.

REM Setup config
echo [4/4] Setting up configuration...
if exist config.json (
    echo ✓ config.json already exists
    echo   → Edit config.json and add your Ford LLM credentials before running
) else (
    if exist config.template.json (
        copy config.template.json config.json >nul
        echo ✓ config.json created from template
        echo   → REQUIRED: Edit config.json and add ford_llm_client_secret
        echo      See COLLEAGUE_SETUP.md Step 3 for details
    ) else (
        echo ⚠ config.json not found - you'll be prompted when app starts
    )
)
echo.

echo ╔════════════════════════════════════════════════════════════════╗
echo ║  Setup Complete!                                                ║
echo ╚════════════════════════════════════════════════════════════════╝
echo.
echo Next steps:
echo   1. Edit config.json and add your Ford LLM client_secret
echo   2. Copy C-BOOK folder (if not already present):
echo      robocopy \\shared\C-BOOK C-BOOK /E
echo   3. Run: python app.py
echo   4. Open: http://localhost:5000
echo.
echo For detailed instructions, see: COLLEAGUE_SETUP.md
echo.
pause
