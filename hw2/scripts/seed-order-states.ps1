param(
    [string]$Buyer = "demo_user",
    [string]$Seller = "demo_seller"
)

$ErrorActionPreference = "Stop"
Push-Location (Split-Path -Parent $PSScriptRoot)
try {
    docker compose exec api python -m app.seed_orders --buyer $Buyer --seller $Seller
    if ($LASTEXITCODE -ne 0) {
        throw "Demo order creation failed."
    }
}
finally {
    Pop-Location
}
