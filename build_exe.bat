@echo off
setlocal
cd /d "%~dp0"
title Build Generic RP Bot Windows EXE

where py >nul 2>nul
if %errorlevel%==0 (
    set "PY=py"
) else (
    where python >nul 2>nul
    if errorlevel 1 (
        echo Python was not found on this BUILD computer.
        echo Install Python 3.11 or 3.12 and try again.
        pause
        exit /b 1
    )
    set "PY=python"
)

if not exist ".buildvenv\Scripts\python.exe" (
    %PY% -m venv .buildvenv
    if errorlevel 1 goto :fail
)

set "BPY=.buildvenv\Scripts\python.exe"
"%BPY%" -m pip install --upgrade pip
if errorlevel 1 goto :fail
"%BPY%" -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto :fail

if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist release rmdir /s /q release

"%BPY%" -m PyInstaller --noconfirm --clean --onefile --console --name RPBot --collect-all discord --collect-all openai --collect-all dotenv --hidden-import aiohttp --hidden-import tzdata src\bot.py
if errorlevel 1 goto :fail
"%BPY%" -m PyInstaller --noconfirm --clean --onefile --console --name RPBot_Setup src\setup_wizard.py
if errorlevel 1 goto :fail

mkdir release
copy /y "dist\RPBot.exe" "release\RPBot.exe" >nul
copy /y "dist\RPBot_Setup.exe" "release\RPBot_Setup.exe" >nul
copy /y "README.md" "release\README_KO.md" >nul
xcopy /e /i /y "config" "release\config" >nul
xcopy /e /i /y "prompts" "release\prompts" >nul

echo.
echo BUILD COMPLETE
echo Distribute the entire release folder.
pause
exit /b 0

:fail
echo.
echo BUILD FAILED
pause
exit /b 1