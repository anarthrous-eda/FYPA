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

REM uv manages the venv + Python version + dependencies. Bail if it isn't on
REM PATH so the build doesn't silently fall back to a stale .venv.
where uv >nul 2>&1
if errorlevel 1 (
    echo ERROR: uv not found on PATH. Install it with: winget install astral-sh.uv
    pause ^& exit /b 1
)

REM Sync runtime + dev + build groups (pulls PyInstaller in via the `build`
REM group). Resolves the lockfile, creates / updates .venv, fetches Python
REM 3.12 if missing.
echo Syncing dependencies (runtime + dev + build) ...
uv sync --group build
if errorlevel 1 (
    echo ERROR: uv sync failed
    pause ^& exit /b 1
)
echo.

REM Wipe previous build artefacts for a clean output
echo Cleaning previous build artefacts...
if exist build  rmdir /s /q build
if exist dist   rmdir /s /q dist
echo.

REM Run PyInstaller using the project spec file (lives next to this script).
REM `uv run` ensures the synced .venv interpreter is used regardless of which
REM shell / activation state the script was launched from.
echo Running PyInstaller ...
echo.
uv run pyinstaller "%~dp0FYPA.spec"

if errorlevel 1 (
    echo.
    echo =========================================
    echo   BUILD FAILED -- see output above
    echo =========================================
    pause ^& exit /b 1
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
    pause ^& exit /b 1
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
