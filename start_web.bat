@echo off
cd /d "%~dp0"
"%~dp0venv\Scripts\python.exe" -X utf8 "%~dp0run_web.py"
if errorlevel 1 pause
