@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (set PY=py) else (set PY=python)

%PY% -m pip install -r requirements-pc.txt "pyinstaller>=6.10,<7"
if errorlevel 1 goto :err

if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

%PY% -m PyInstaller --noconfirm --clean --windowed --onedir ^
  --name "Przewijak_ARTYKULY_PC" ^
  --icon "assets\przewijak.ico" ^
  --collect-all webview ^
  --collect-all PySide6 ^
  --exclude-module clr ^
  --exclude-module pythonnet ^
  --exclude-module clr_loader ^
  desktop_app.py
if errorlevel 1 goto :err

echo.
echo GOTOWE: dist\Przewijak_ARTYKULY_PC\Przewijak_ARTYKULY_PC.exe
echo Backend okna: Qt / PySide6 - bez pythonnet.
pause
exit /b 0

:err
echo.
echo BLAD BUDOWANIA EXE.
pause
exit /b 1
