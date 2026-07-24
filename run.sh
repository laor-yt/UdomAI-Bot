#!/bin/bash
echo "==================================="
echo "       Starting UdomAI-Bot"
echo "==================================="

# Check if Python 3 is installed
if ! command -v python3 &> /dev/null
then
    echo "Python 3 could not be found."
    echo "Please install Python 3.10 or newer and try again."
    exit 1
fi

# Create virtual environment if it doesn't exist
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

# Activate virtual environment
echo "Activating virtual environment..."
source venv/bin/activate

# Install requirements
echo "Installing dependencies..."
pip install -r requirements.txt

# Check if .env file exists
if [ ! -f ".env" ]; then
    echo "Warning: .env file not found!"
    echo "Please create a .env file and add your TELEGRAM_BOT_TOKEN, API_ID, and API_HASH."
fi

# Run the bot
echo "Running the bot..."
python3 main.py
