@echo off
rem ===================================================================
rem  Applies a build that was made while the game was open.
rem
rem  Windows will not let a running .exe be replaced, so builds made
rem  during play land as Pixel_RPG.new.exe. Close the game, run this,
rem  and it swaps the new build in.
rem ===================================================================
setlocal
cd /d "%~dp0"

if not exist "%~dp0Pixel_RPG.new.exe" (
    echo Nothing to apply - Pixel_RPG.exe is already the latest build.
    echo.
    pause
    exit /b 0
)

tasklist /FI "IMAGENAME eq Pixel_RPG.exe" 2>nul | find /I "Pixel_RPG.exe" >nul
if %errorlevel%==0 (
    echo Pixel RPG is still running.
    echo Close the game window first, then run this again.
    echo.
    pause
    exit /b 1
)

if exist "%~dp0Pixel_RPG.exe" del "%~dp0Pixel_RPG.exe"
if exist "%~dp0Pixel_RPG.exe" (
    echo Could not replace Pixel_RPG.exe - something still has it open.
    echo.
    pause
    exit /b 1
)

ren "%~dp0Pixel_RPG.new.exe" "Pixel_RPG.exe"
echo Updated. Your saves are untouched - run run.bat to play.
echo.
pause
exit /b 0
