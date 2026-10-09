@echo off
rem Starts recover.py with administrator rights (needed to read the drive).
net session >nul 2>&1
if %errorlevel% neq 0 (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
cd /d "%~dp0"
where py >nul 2>&1
if %errorlevel%==0 (
    py -3 "%~dp0recover.py"
) else (
    python "%~dp0recover.py"
)
pause
