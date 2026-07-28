$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$VenvDir = Join-Path $Root ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"

function Test-SupportedPython([string]$Executable) {
    if (-not (Test-Path $Executable)) {
        return $false
    }
    & $Executable -c "import pip, sys; raise SystemExit(0 if (sys.version_info >= (3, 11) and sys.version_info < (3, 14)) else 1)"
    return $LASTEXITCODE -eq 0
}

if (-not (Test-SupportedPython $VenvPython)) {
    $Uv = Get-Command uv -ErrorAction SilentlyContinue
    if ($null -eq $Uv) {
        throw "Python 3.11-3.13 is required. Install uv from https://docs.astral.sh/uv/ or install a compatible Python, then rerun this script."
    }

    # This only recreates this project's .venv. It does not alter the system Python.
    & $Uv.Source venv --clear --seed --python 3.11 $VenvDir
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create the project virtual environment with Python 3.11."
    }
}

& $VenvPython -m pip install -e "$Root[dev]"
if ($LASTEXITCODE -ne 0) {
    throw "Dependency installation failed. The project virtual environment is not ready."
}

& $VenvPython -c "import aicf"
if ($LASTEXITCODE -ne 0) {
    throw "Dependency installation finished but the application cannot be imported."
}

Write-Host "Environment ready: $VenvPython"
