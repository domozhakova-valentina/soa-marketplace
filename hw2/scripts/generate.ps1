$ErrorActionPreference = "Stop"

Push-Location (Split-Path -Parent $PSScriptRoot)

try {
    docker compose --profile tools run --rm codegen

    if ($LASTEXITCODE -ne 0) {
        throw "OpenAPI generation failed."
    }
}
finally {
    Pop-Location
}