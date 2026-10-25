@echo off
cd /d "%~dp0"
if not exist .venv py -3 -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\pyinstaller --noconfirm --windowed --name NAS115Sync --collect-submodules watchdog --collect-submodules oss2 NAS115Sync.pyw
if errorlevel 1 (
  echo Build failed.
  pause
  exit /b 1
)
echo.
echo Built: dist\NAS115Sync\NAS115Sync.exe
pause
