param([string]$ComposeProject)
$ErrorActionPreference = "Stop"
$savedOutputEncoding = $OutputEncoding
Push-Location (Split-Path -Parent $PSScriptRoot)
try {
    $OutputEncoding = New-Object System.Text.UTF8Encoding($false)
    $options = @()
    if ($ComposeProject) { $options += @("-p", $ComposeProject) }
    $dbUser = (& docker compose @options exec -T db printenv POSTGRES_USER | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) { throw "Cannot read PostgreSQL configuration." }
    $dbName = (& docker compose @options exec -T db printenv POSTGRES_DB | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) { throw "Cannot read PostgreSQL configuration." }
    Get-Content -LiteralPath (Join-Path $PSScriptRoot "inspect-db.sql") -Raw |
        & docker compose @options exec -T db psql -X -U $dbUser -d $dbName -v ON_ERROR_STOP=1
    if ($LASTEXITCODE -ne 0) { throw "Database inspection failed." }
}
finally { $OutputEncoding = $savedOutputEncoding; Pop-Location }
