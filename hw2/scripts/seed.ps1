$ErrorActionPreference = "Stop"
Push-Location (Split-Path -Parent $PSScriptRoot)
try {
    docker compose exec api python -m app.seed_users
    if ($LASTEXITCODE -ne 0) {
        throw "Demo account creation failed."
    }
}
finally {
    Pop-Location
}
