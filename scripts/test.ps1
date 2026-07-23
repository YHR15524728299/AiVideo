$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"

if (-not (Test-Path $Python)) {
    throw "虚拟环境不存在，请先运行 scripts\bootstrap.ps1"
}

Push-Location $Root
try {
    & $Python -m pytest --cov=aicf --cov-report=term-missing
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
