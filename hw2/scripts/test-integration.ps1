$ErrorActionPreference = "Stop"

Push-Location (Split-Path -Parent $PSScriptRoot)

try {
    docker compose --profile tools run --build --rm test-integration

    if ($LASTEXITCODE -ne 0) {
        throw "PostgreSQL integration tests failed."
    }
}
finally {
    Pop-Location
}
