@echo off
cd /d "%~dp0"
set "PYTHONPATH=%~dp0src"
set "AICF_PROJECT_ROOT=%~dp0"
start "" ".venv\Scripts\pythonw.exe" -c "from aicf.gui import launch; launch()"