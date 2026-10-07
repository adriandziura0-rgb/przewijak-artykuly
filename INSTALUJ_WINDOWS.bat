@echo off
setlocal
cd /d "%~dp0"
echo.
echo ===============================================
echo  PRZEWIJAK ARTYKULY V15.1 PC APP - WINDOWS
echo ===============================================
echo.
where py >nul 2>nul
if %errorlevel%==0 (
  set PY=py
) else (
  set PY=python
)
%PY% -m pip install -r requirements-pc.txt
if errorlevel 1 goto :pip_error

echo.
echo Instalacja OPCJONALNEGO fallbacku JavaScript dla TVP Info...
%PY% -m pip install "playwright>=1.48,<2"
if errorlevel 1 goto :optional_warning
%PY% -m playwright install chromium
if errorlevel 1 goto :optional_warning

echo.
echo GOTOWE. Uruchom START_WINDOWS.bat
echo Program otworzy sie we wlasnym oknie, bez panelu w zewnetrznej przegladarce.
pause
exit /b 0

:optional_warning
echo.
echo UWAGA: Playwright/Chromium nie zainstalowal sie.
echo Program nadal dziala: TVP najpierw uzywa lekkiego API i statycznego GET.
echo JavaScript jest tylko awaryjnym fallbackiem na Windows.
pause
exit /b 0

:pip_error
echo.
echo BLAD instalacji podstawowych zaleznosci.
echo Sprawdz instalacje Pythona i polaczenie z internetem.
pause
exit /b 1
