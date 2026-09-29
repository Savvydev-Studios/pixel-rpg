@echo off
rem ===================================================================
rem  Pixel RPG launcher
rem
rem  Works from anywhere: every path below is resolved relative to this
rem  file (%~dp0), never to whatever folder the console happens to be in.
rem  Copy or move the whole folder wherever you like - including onto a
rem  different PC - and this still works.
rem
rem  Prefers the bundled Pixel_RPG.exe, which needs nothing installed.
rem  Falls back to running from source if you have Python + pygame.
rem ===================================================================
setlocal
cd /d "%~dp0"

if exist "%~dp0Pixel_RPG.exe" (
    echo Starting Pixel RPG...
    start "Pixel RPG" "%~dp0Pixel_RPG.exe"
    exit /b 0
)

echo Pixel_RPG.exe not found next to this script.
echo Trying to run from source instead...
echo.

where py >nul 2>nul
if %errorlevel%==0 (
    py -3 "%~dp0main.py"
    goto :done
)

where python >nul 2>nul
if %errorlevel%==0 (
    python "%~dp0main.py"
    goto :done
)

echo.
echo Could not find Pixel_RPG.exe or a Python installation.
echo.
echo   * Easiest fix: keep Pixel_RPG.exe in this folder and run this file again.
echo   * Or install Python 3.8+ from python.org, then run:
echo         pip install -r "%~dp0requirements.txt"
echo.
pause
exit /b 1

:done
if %errorlevel% neq 0 (
    echo.
    echo Pixel RPG exited with an error ^(code %errorlevel%^).
    echo If pygame is missing, run:  pip install -r "%~dp0requirements.txt"
    echo.
    pause
)
exit /b %errorlevel%
