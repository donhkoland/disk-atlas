@echo off
cd /d "%~dp0"
py -3 -m venv .venv
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -c "from atlas.storage import init; init()"
if errorlevel 1 goto fail
echo Setup complete. Double-click START.bat.
pause
exit /b 0
:fail
echo Setup failed. Install Python 3.10 or newer, then try again.
pause
exit /b 1
