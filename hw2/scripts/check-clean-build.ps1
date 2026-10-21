param([switch]$RunDemo, [switch]$NoCache)
$ErrorActionPreference = "Stop"
$sourceDirectory = Split-Path -Parent $PSScriptRoot
$projectName = "hw2-check-" + [guid]::NewGuid().ToString("N").Substring(0, 12)
$copyDirectory = Join-Path $sourceDirectory ".venv/verification/$projectName"
$null = New-Item -ItemType Directory -Path $copyDirectory -Force

# Only source files are copied. Existing generated code, .env, .venv and Git
# metadata cannot contribute to this build. No existing files are deleted.
foreach ($entry in @("app", "openapi", "migrations", "scripts", "tests", "Dockerfile", "compose.yaml",
                     ".dockerignore", ".env.example", "requirements.txt", "requirements-test.txt", "pytest.ini", "README.md")) {
    Copy-Item -LiteralPath (Join-Path $sourceDirectory $entry) -Destination $copyDirectory -Recurse
}
if (Test-Path -LiteralPath (Join-Path $copyDirectory "generated")) { throw "Clean copy contains generated code." }

$savedEnvironment = @{}
$testConfiguration = @{
    API_PORT = "0"; POSTGRES_DB = "clean_marketplace"; POSTGRES_USER = "clean_marketplace"
    POSTGRES_PASSWORD = [guid]::NewGuid().ToString("N")
    JWT_SECRET = [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
    ORDER_CREATE_INTERVAL_MINUTES = "5"; ORDER_UPDATE_INTERVAL_MINUTES = "5"
}
$oldDemo = Get-Variable -Name MarketplaceDemo -Scope Global -ErrorAction SilentlyContinue
$oldDemoValue = if ($oldDemo) { $oldDemo.Value } else { $null }
$composeArguments = @("-p", $projectName, "--project-directory", $copyDirectory,
                       "--env-file", (Join-Path $copyDirectory ".env"), "-f", (Join-Path $copyDirectory "compose.yaml"))
function Invoke-CleanCompose {
    param([string[]]$Arguments)
    & docker compose @composeArguments @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Clean-build Docker command failed." }
}

$passed = $false
$previousOutputEncoding = $OutputEncoding
try {
    $OutputEncoding = New-Object System.Text.UTF8Encoding($false)
    foreach ($name in $testConfiguration.Keys) {
        $savedEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
        [Environment]::SetEnvironmentVariable($name, $testConfiguration[$name], "Process")
    }
    $lines = foreach ($name in $testConfiguration.Keys) { "$name=$($testConfiguration[$name])" }
    [IO.File]::WriteAllLines((Join-Path $copyDirectory ".env"), [string[]]$lines)
    Write-Host "Clean source copy: $copyDirectory"
    Write-Host "Isolated Compose project: $projectName"
    $buildArguments = @("build")
    if ($NoCache) { $buildArguments += "--no-cache" }
    Invoke-CleanCompose -Arguments ($buildArguments + "api")
    Invoke-CleanCompose -Arguments @("up", "-d", "--wait", "--wait-timeout", "120", "api")
    $binding = (Invoke-CleanCompose -Arguments @("port", "api", "8000") | Select-Object -First 1).Trim()
    if ($binding -notmatch ':(\d+)$') { throw "Cannot determine isolated API port." }
    $url = "http://127.0.0.1:" + $Matches[1]
    $healthy = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        try {
            $health = Invoke-RestMethod -Uri "$url/health" -TimeoutSec 2
            if ($health.status -eq "ok") { $healthy = $true; break }
        }
        catch { }
        Start-Sleep -Milliseconds 500
    }
    if (-not $healthy) { throw "Clean-built API did not become ready." }
    $migrationCount = (Invoke-CleanCompose -Arguments @("exec", "-T", "db", "psql", "-X", "-U", "clean_marketplace", "-d", "clean_marketplace", "-At", "-c", "SELECT count(*) FROM flyway_schema_history WHERE success") | Out-String).Trim()
    if ([int]$migrationCount -ne 3) { throw "Expected all three Flyway migrations." }
    Invoke-CleanCompose -Arguments @("exec", "-T", "api", "python", "-c", "from marketplace_api.models.product_create import ProductCreate; print('Generated DTO import OK')")
    if ($RunDemo) {
        $seedPassword = [guid]::NewGuid().ToString("N") + "_A1"
        $seedPassword | & docker compose @composeArguments exec -T api python -c "import sys; from app.seed_users import seed_demo_users; from app.db.connection import get_engine; pw=sys.stdin.buffer.read().decode('utf-8-sig').strip(); assert pw.isascii() and len(pw)==35, 'Unexpected stdin password encoding'; seed_demo_users(get_engine(), pw); print('Demo accounts prepared')"
        if ($LASTEXITCODE -ne 0) { throw "Isolated demo account creation failed." }
        $securePassword = ConvertTo-SecureString -String $seedPassword -AsPlainText -Force
        $seedPassword = $null
        $demoScript = Join-Path $copyDirectory "scripts/demo.ps1"
        & $demoScript -Step Login -BaseUrl $url -Password $securePassword -ComposeProject $projectName
        foreach ($step in @("Products", "Promos", "Order", "RateLimit", "ActiveOrder", "Rollback", "Update", "Cancel", "Access", "Tokens", "States", "Inspect", "Logs")) {
            Write-Host "=== Demo step: $step ==="
            & $demoScript -Step $step
        }
        $securePassword.Dispose()
    }
    $passed = $true
}
finally {
    try {
        # Only the new, uniquely named test project and its new data are removed.
        if ($projectName -notmatch '^hw2-check-[0-9a-f]{12}$') { throw "Invalid isolated project name." }
        if (-not $passed) { & docker compose @composeArguments logs --tail 50 }
        & docker compose @composeArguments down --volumes --remove-orphans
        if ($LASTEXITCODE -ne 0) { Write-Warning "Could not clean up isolated project $projectName." }
    }
    finally {
        foreach ($name in $savedEnvironment.Keys) {
            [Environment]::SetEnvironmentVariable($name, $savedEnvironment[$name], "Process")
        }
        $OutputEncoding = $previousOutputEncoding
        if ($oldDemo) { $global:MarketplaceDemo = $oldDemoValue }
        else { Remove-Variable -Name MarketplaceDemo -Scope Global -ErrorAction SilentlyContinue }
    }
}
Write-Host "Clean build passed. All three migrations and HTTP health check succeeded."
if ($RunDemo) { Write-Host "All demo steps passed against the isolated API and PostgreSQL." }
