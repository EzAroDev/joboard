@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

rem --- Python disponible ? (python, sinon lanceur py) ---
set "PY=python"
%PY% --version >nul 2>&1
if errorlevel 1 (
  set "PY=py"
  py --version >nul 2>&1
  if errorlevel 1 (
    echo [ERREUR] Python est introuvable. Installez-le depuis https://www.python.org/downloads/
    echo          en cochant "Add python.exe to PATH", puis relancez build.bat.
    pause
    exit /b 1
  )
)

echo === 1/4 Installation des dependances ===
%PY% -m pip install --upgrade -r requirements.txt pyinstaller pillow
if errorlevel 1 goto :fail

echo === 2/4 Icone ===
%PY% make_icon.py
if errorlevel 1 goto :fail
copy /Y icon.ico web\favicon.ico >nul

echo === 3/4 Compilation (avec terminal) ===
%PY% -m PyInstaller --noconfirm --onefile --console --name "Joboard" --icon icon.ico --add-data "web;web" server.py
if errorlevel 1 goto :fail

echo === 4/4 Copie de la configuration dans dist ===
if not exist "dist\config.json" copy /Y config.json "dist\config.json" >nul
for %%F in (credentials.json service_account.json token.json) do (
  if exist "%%F" if not exist "dist\%%F" copy /Y "%%F" "dist\%%F" >nul
)

echo.
echo Termine : "%~dp0dist\Joboard.exe"
explorer "%~dp0dist"
pause
exit /b 0

:fail
echo.
echo [ERREUR] La construction a echoue. Lisez les messages ci-dessus.
pause
exit /b 1
