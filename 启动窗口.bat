@echo off
chcp 65001 >nul
cd /d "%~dp0"
".venv\Scripts\python.exe" -c "import aicf" >nul 2>&1
if errorlevel 1 (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\bootstrap.ps1"
    if errorlevel 1 (
        echo Setup failed. See the error above.
        pause
        exit /b 1
    )
)
set "PYTHONPATH=%~dp0src"
set "AICF_PROJECT_ROOT=%~dp0"
start "" ".venv\Scripts\pythonw.exe" -c "from aicf.gui import launch; launch()"
