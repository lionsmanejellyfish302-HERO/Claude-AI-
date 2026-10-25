@echo off
rem Command line: run_cli.bat scan / sync / reviews / watch ...
cd /d "%~dp0"
.venv\Scripts\python.exe -m nas115sync %*
