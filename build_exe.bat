@echo off
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  echo Python 3.10+ is required: https://www.python.org/downloads/
  pause
  exit /b 1
)
if not exist .venv py -3 -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\pyinstaller --noconfirm NAS115Sync.spec
if errorlevel 1 (
  echo Build failed.
  pause
  exit /b 1
)
echo.
echo Self-test of the new exe (simulated 115, about 30 seconds)...
dist\NAS115Sync-CLI.exe --self-test "%TEMP%\nas115sync-selftest" --no-gui
if errorlevel 1 (
  echo SELF-TEST FAILED. See %TEMP%\nas115sync-selftest\selftest_report.json
  pause
  exit /b 1
)
echo.
echo Built and verified:
echo   dist\NAS115Sync.exe       (GUI)
echo   dist\NAS115Sync-CLI.exe   (command line)
pause
