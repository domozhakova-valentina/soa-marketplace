$ErrorActionPreference = "Stop"

$response = Invoke-RestMethod `
    -Method Get `
    -Uri "http://localhost:8000/health"

if ($response.status -ne "ok") {
    throw "Health check failed."
}

Write-Output "Health check passed."