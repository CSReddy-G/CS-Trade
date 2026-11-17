@echo off
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
cls

echo Starting Kite Bot Setup...

:: Check if Python works
python --version
if errorlevel 1 goto NOPYTHON

:: Run the environment check directly
if exist .venv\Scripts\python.exe goto INSTALLDEPS

echo Creating virtual environment...
python -m venv .venv
if errorlevel 1 goto ENVFAIL

:INSTALLDEPS
if exist .venv\.deps_ok goto STARTAPP

echo Installing requirements (1-2 minutes)...
.venv\Scripts\python -m pip install -q --upgrade pip
.venv\Scripts\python -m pip install -r requirements.txt
if errorlevel 1 goto PIPFAIL
echo. > .venv\.deps_ok

:STARTAPP
echo.
echo Opening control panel at http://127.0.0.1:8765
echo Keep this window open while the bot runs. Close it to shut down.
echo.
.venv\Scripts\python app.py
goto END

:NOPYTHON
echo ERROR: Python 3 is not installed or not added to PATH.
goto END

:ENVFAIL
echo ERROR: Could not create virtual environment.
goto END

:PIPFAIL
echo ERROR: Installation failed. Check your internet connection.
goto END

:END
echo.
echo Script stopped.
pause
