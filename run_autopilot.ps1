param(
    [string]$Job = ("JOB-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"

if (-not (Test-Path $Python)) {
    & (Join-Path $Root "scripts\bootstrap.ps1")
}

Push-Location $Root
try {
    & $Python -m aicf doctor
    if ($LASTEXITCODE -ne 0) {
        throw "Doctor failed. Fix required capabilities and retry."
    }
    & $Python -m aicf autopilot --job $Job
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Status: FAILED_NEEDS_ATTENTION"
        Write-Host "Recovery command: .\run_autopilot.ps1 -Job `"$Job`""
        exit $LASTEXITCODE
    }
}
catch {
    Write-Error $_
    Write-Host "Status: FAILED_NEEDS_ATTENTION"
    Write-Host "Doctor recovery command: .\scripts\doctor.ps1"
    Write-Host "Pipeline recovery command: .\run_autopilot.ps1 -Job `"$Job`""
    exit 1
}
finally {
    Pop-Location
}
