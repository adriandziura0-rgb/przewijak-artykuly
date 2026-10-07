@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py desktop_app.py
) else (
  python desktop_app.py
)
if errorlevel 1 pause
