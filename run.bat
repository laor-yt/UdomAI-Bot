@echo off
echo ===================================
echo     Starting UdomAI-Bot
echo ===================================

:: Check if Python is installed
python --version >nul 2>&1
IF %ERRORLEVEL% NEQ 0 (
    echo Python is not installed or not in your PATH.
    echo Please install Python 3.11 or newer and try again.
    pause
    exit /b
)

:: Create virtual environment if it doesn't exist
IF NOT EXIST "venv" (
    echo Creating virtual environment...
    python -m venv venv
)

:: Activate virtual environment
echo Activating virtual environment...
call venv\Scripts\activate.bat

:: Install requirements
echo Installing dependencies...
python -m pip install --upgrade pip
pip install -r requirements.txt

:: Check if .env file exists
IF NOT EXIST ".env" (
    echo Warning: .env file not found!
    echo Please create a .env file and add your TELEGRAM_BOT_TOKEN, API_ID, and API_HASH.
    pause
)

:: Ensure SERVER_ID is set in .env
findstr /C:"SERVER_ID=" .env >nul
IF %ERRORLEVEL% NEQ 0 (
    echo.
    echo ----------------------------------------------------
    echo First time setup: What is the ID of this server?
    echo If this is your main server, type 'main'.
    echo If this is a secondary server, type 'server_1', etc.
    echo ----------------------------------------------------
    set /p NEW_SERVER_ID="Enter SERVER_ID: "
    echo SERVER_ID=%NEW_SERVER_ID%>> .env
    echo Saved SERVER_ID=%NEW_SERVER_ID% to .env!
)

:: Run the bot
echo Running the bot...
python main.py

pause
