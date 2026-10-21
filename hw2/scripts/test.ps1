$ErrorActionPreference = "Stop"

Push-Location (Split-Path -Parent $PSScriptRoot)

try {
    docker compose --profile tools run --build --rm test

    if ($LASTEXITCODE -ne 0) {
        throw "Generated API contract tests failed."
    }
}
finally {
    Pop-Location
}
