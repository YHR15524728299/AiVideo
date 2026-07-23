$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Get-Command python -ErrorAction Stop

if (-not (Test-Path (Join-Path $Root ".venv\Scripts\python.exe"))) {
    try {
        & $Python.Source -m venv (Join-Path $Root ".venv")
    }
    catch {
        & $Python.Source -m pip install virtualenv
        & $Python.Source -m virtualenv (Join-Path $Root ".venv")
    }
}

$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
& $VenvPython -m pip install -e "$Root[dev]"
Write-Host "环境已就绪: $VenvPython"
