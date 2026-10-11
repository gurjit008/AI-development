$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $projectRoot
try {
    & python scripts/verify.py
    if ($LASTEXITCODE -ne 0) {
        throw "Project verification failed (exit code $LASTEXITCODE)."
    }
}
finally {
    Pop-Location
}
