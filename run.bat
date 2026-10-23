@echo off
cd /d %~dp0

where python >nul 2>nul
if errorlevel 1 (
    echo Python 3 was not found on this PC.
    echo Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
    pause
    exit /b 1
)

python -m pip install -q -r requirements.txt
if errorlevel 1 (
    echo Could not install the required packages. See the message above.
    pause
    exit /b 1
)

start "" pythonw memorial_slideshow.py
