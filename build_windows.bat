@echo off
setlocal enabledelayedexpansion

REM ============================================================
REM Excogitare Windows x64 Build
REM Erstellt:
REM   dist\Excogitare\
REM   release\win64\Excogitare-win64.zip
REM ============================================================

cd /d "%~dp0"

echo.
echo === Excogitare Windows Build ===
echo Projekt: %CD%
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo FEHLER: Python wurde nicht gefunden.
    pause
    exit /b 1
)

python -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo PyInstaller ist nicht installiert.
    echo Installation mit:
    echo   python -m pip install pyinstaller
    pause
    exit /b 1
)

if exist "build" (
    echo Entferne altes build-Verzeichnis...
    rmdir /s /q "build"
)

if exist "dist\Excogitare" (
    echo Entferne alten Excogitare-Build...
    rmdir /s /q "dist\Excogitare"
)

if not exist "release\win64" (
    mkdir "release\win64"
)

echo.
echo Starte PyInstaller...
echo.

python -m PyInstaller ^
    --noconsole ^
    --name Excogitare ^
    --clean ^
    "run_mindmapper.py"

if errorlevel 1 (
    echo.
    echo FEHLER: PyInstaller-Build fehlgeschlagen.
    pause
    exit /b 1
)

if exist "examples" (
    echo.
    echo Kopiere examples...
    if exist "dist\Excogitare\examples" rmdir /s /q "dist\Excogitare\examples"
    xcopy "examples" "dist\Excogitare\examples\" /E /I /Y >nul
)

set "ZIP=release\win64\Excogitare-win64.zip"

if exist "%ZIP%" del /q "%ZIP%"

echo.
echo Erzeuge ZIP:
echo   %ZIP%
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "Compress-Archive -Path 'dist\Excogitare\*' -DestinationPath '%ZIP%' -Force"

if errorlevel 1 (
    echo.
    echo FEHLER: ZIP konnte nicht erzeugt werden.
    pause
    exit /b 1
)

echo.
echo ==========================================
echo BUILD ERFOLGREICH
echo.
echo EXE:
echo   dist\Excogitare\Excogitare.exe
echo.
echo ZIP fuer Betatester:
echo   %ZIP%
echo ==========================================
echo.

pause
endlocal
