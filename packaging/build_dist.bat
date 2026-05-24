@echo off
setlocal EnableDelayedExpansion

echo =========================================
echo   FYPA  --  Build Distribution
echo =========================================
echo.

REM This script lives in packaging\. Switch to the repo root (its parent) so
REM every relative path below (.venv, build, dist, README.md) resolves there
REM no matter where the script was launched from.
cd /d "%~dp0.."

if not exist ".venv\Scripts\activate.bat" (
    echo ERROR: .venv not found at the repo root. Create the venv there first.
    pause & exit /b 1
)

REM Activate the virtual environment
call .venv\Scripts\activate.bat
if errorlevel 1 (
    echo ERROR: Failed to activate .venv
    pause & exit /b 1
)

REM Reconcile the venv against requirements.txt so a freshly-built .venv (or
REM one missing a recently-added dep like lxml for the ParaView exporter)
REM picks up everything before PyInstaller does its dependency walk.
if exist "requirements.txt" (
    echo Reconciling .venv against requirements.txt ...
    pip install -r requirements.txt
    if errorlevel 1 (
        echo ERROR: pip install -r requirements.txt failed
        pause & exit /b 1
    )
    echo.
)

REM Install PyInstaller into the venv if it is not already present
python -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo PyInstaller not found -- installing into .venv ...
    pip install pyinstaller
    if errorlevel 1 (
        echo ERROR: pip install pyinstaller failed
        pause & exit /b 1
    )
    echo.
)

REM Wipe previous build artefacts for a clean output
echo Cleaning previous build artefacts...
if exist build  rmdir /s /q build
if exist dist   rmdir /s /q dist
echo.

REM Run PyInstaller using the project spec file (lives next to this script)
echo Running PyInstaller ...
echo.
pyinstaller "%~dp0FYPA.spec"

if errorlevel 1 (
    echo.
    echo =========================================
    echo   BUILD FAILED -- see output above
    echo =========================================
    pause & exit /b 1
)

REM Copy README into the dist folder so it travels with the zip
if exist "README.md" (
    copy /y "README.md" "dist\FYPA\README.md" >nul
)

REM Remove the intermediate build folder -- not needed for distribution
echo Removing intermediate build folder...
if exist build rmdir /s /q build

REM Package dist\FYPA\ into a single zip for sharing
echo Creating distribution zip...
if exist "dist\FYPA.zip" del /q "dist\FYPA.zip"
powershell -NoProfile -Command "Compress-Archive -Path 'dist\FYPA' -DestinationPath 'dist\FYPA.zip' -Force"
if errorlevel 1 (
    echo ERROR: Failed to create dist\FYPA.zip
    pause & exit /b 1
)

REM Remove the unzipped staging folder -- only the zip should remain in dist\
echo Cleaning staging folder...
if exist "dist\FYPA" rmdir /s /q "dist\FYPA"

echo.
echo =========================================
echo   BUILD COMPLETE
echo.
echo   Distribution zip:  dist\FYPA.zip
echo.
echo   To distribute: send dist\FYPA.zip.
echo   The recipient extracts the zip and
echo   runs FYPA.exe inside the extracted
echo   FYPA folder. README.md is included.
echo =========================================
echo.
pause
