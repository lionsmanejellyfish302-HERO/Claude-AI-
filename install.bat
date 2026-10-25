@echo off
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  echo Python 3.10+ is required: https://www.python.org/downloads/  ^(check "Add python.exe to PATH"^)
  pause
  exit /b 1
)
if not exist .venv py -3 -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r requirements.txt
echo.
echo Done. Double-click run_gui.bat to start NAS115Sync.
pause
