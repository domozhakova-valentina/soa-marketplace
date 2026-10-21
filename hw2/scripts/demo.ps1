param(
    [ValidateSet("Help", "Login", "Products", "Promos", "PromoFixtures", "Order", "RateLimit", "ActiveOrder", "Rollback", "Update", "Cancel", "Access", "Tokens", "States", "Inspect", "Logs")]
    [string]$Step = "Help",
    [string]$BaseUrl = "http://localhost:8000",
    [System.Security.SecureString]$Password,
    [string]$ComposeProject
)
$ErrorActionPreference = "Stop"
$projectDirectory = Split-Path -Parent $PSScriptRoot

# Session memory only. No passwords or tokens are written to disk.
function Get-DemoState {
    if (-not (Get-Variable -Name MarketplaceDemo -Scope Global -ErrorAction SilentlyContinue)) {
        throw "Run .\scripts\demo.ps1 -Step Login first, in this PowerShell window."
    }
    return $global:MarketplaceDemo
}

# Also available in the interactive terminal for manually edited requests.
function global:Invoke-MarketplaceRequest {
    param([string]$Method = "GET", [string]$Path, [string]$Token,
          $Body, [int]$ExpectedStatus = 200, [string]$ExpectedError, [switch]$Quiet)
    if (-not $global:MarketplaceDemo) { throw "Run Login first." }
    if (-not $Path.StartsWith("/")) { throw "Path must start with /." }
    $request = [System.Net.HttpWebRequest]::Create($global:MarketplaceDemo.BaseUrl.TrimEnd('/') + $Path)
    $request.Method = $Method
    $request.Accept = "application/json"
    $request.Timeout = 15000
    if ($Token) { $request.Headers["Authorization"] = "Bearer " + $Token }
    if ($PSBoundParameters.ContainsKey("Body")) {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes(($Body | ConvertTo-Json -Depth 20 -Compress))
        $request.ContentType = "application/json; charset=utf-8"
        $request.ContentLength = $bytes.Length
        $stream = $request.GetRequestStream()
        try { $stream.Write($bytes, 0, $bytes.Length) } finally { $stream.Dispose() }
    }
    $response = $null
    try {
        try { $response = $request.GetResponse() }
        catch [System.Net.WebException] {
            if ($null -eq $_.Exception.Response) { throw }
            $response = $_.Exception.Response
        }
        $reader = New-Object System.IO.StreamReader($response.GetResponseStream())
        try { $text = $reader.ReadToEnd() } finally { $reader.Dispose() }
        $data = if ($text) { $text | ConvertFrom-Json } else { $null }
        $status, $requestId = [int]$response.StatusCode, $response.Headers["X-Request-Id"]
        if (-not $Quiet) {
            Write-Host "$Method $Path -> HTTP $status; request_id=$requestId"
            if ($null -ne $data) { Write-Host ($data | ConvertTo-Json -Depth 20) }
        }
        if ($status -ne $ExpectedStatus) { throw "Expected HTTP $ExpectedStatus for $Method $Path, got $status. $text" }
        if ($ExpectedError -and $data.error_code -ne $ExpectedError) { throw "Expected $ExpectedError, got $($data.error_code)." }
        return [pscustomobject]@{ Status = $status; Data = $data; RequestId = $requestId }
    }
    finally { if ($null -ne $response) { $response.Dispose() } }
}

function Invoke-DemoCompose {
    param([string[]]$Arguments)
    $state = Get-DemoState
    Push-Location $state.ProjectDirectory
    try {
        $options = @("--project-directory", $state.ProjectDirectory, "-f", (Join-Path $state.ProjectDirectory "compose.yaml"))
        if ($state.ComposeProject) { $options += @("-p", $state.ComposeProject) }
        & docker compose @options @Arguments
        if ($LASTEXITCODE -ne 0) { throw "Docker Compose command failed." }
    }
    finally { Pop-Location }
}

function Invoke-DemoSql {
    param([string]$Sql, [hashtable]$Variables = @{}, [switch]$Scalar)
    $state = Get-DemoState
    if (-not $state.DbUser) {
        $state.DbUser = (Invoke-DemoCompose -Arguments @("exec", "-T", "db", "printenv", "POSTGRES_USER") | Out-String).Trim()
        $state.DbName = (Invoke-DemoCompose -Arguments @("exec", "-T", "db", "printenv", "POSTGRES_DB") | Out-String).Trim()
    }
    $arguments = @("exec", "-T", "db", "psql", "-X", "-U", $state.DbUser, "-d", $state.DbName, "-v", "ON_ERROR_STOP=1")
    if ($Scalar) { $arguments += "-At" }
    foreach ($name in $Variables.Keys) { $arguments += @("-v", "$name=$($Variables[$name])") }
    $savedOutputEncoding = $OutputEncoding
    Push-Location $state.ProjectDirectory
    try {
        $OutputEncoding = New-Object System.Text.UTF8Encoding($false)
        $options = @("--project-directory", $state.ProjectDirectory, "-f", (Join-Path $state.ProjectDirectory "compose.yaml"))
        if ($state.ComposeProject) { $options += @("-p", $state.ComposeProject) }
        $output = $Sql | & docker compose @options @arguments
        if ($LASTEXITCODE -ne 0) { throw "Demo SQL failed." }
        if ($Scalar) { return ($output | Out-String).Trim() }
        $output | ForEach-Object { Write-Host $_ }
    }
    finally { $OutputEncoding = $savedOutputEncoding; Pop-Location }
}

function New-DemoBuyer {
    $username = "defense_" + [guid]::NewGuid().ToString("N").Substring(0, 16)
    $temporaryPassword = [guid]::NewGuid().ToString("N") + "_A1"
    $registered = Invoke-MarketplaceRequest -Method POST -Path "/auth/register" -Body @{ username = $username; password = $temporaryPassword } -ExpectedStatus 201
    $pair = Invoke-MarketplaceRequest -Method POST -Path "/auth/login" -Body @{ username = $username; password = $temporaryPassword } -Quiet
    return @{ Username = $username; Id = $registered.Data.id; Access = $pair.Data.access_token; Refresh = $pair.Data.refresh_token }
}

function Get-DemoProduct {
    param([string]$Id)
    return (Invoke-MarketplaceRequest -Path "/products/$Id" -Token (Get-DemoState).Tokens.User -Quiet).Data
}

function Get-DemoPromoUses {
    param([string]$Code)
    return [int](Invoke-DemoSql -Sql "SELECT current_uses FROM promo_codes WHERE code = :'code';" -Variables @{ code = $Code } -Scalar)
}

function Assert-Demo {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

if ($Step -eq "Help") {
    Write-Host "Run steps in the same PowerShell window:"
    Write-Host "Login, Products, Promos, Order, RateLimit, ActiveOrder, Rollback, Update, Cancel, Access, Tokens, States, Inspect, Logs"
    Write-Host "Example: .\scripts\demo.ps1 -Step Login"
    Write-Host "Create demo_seller/demo_admin with scripts/seed.ps1 first."
    return
}

if ($Step -eq "Login") {
    $global:MarketplaceDemo = @{
        BaseUrl = $BaseUrl; ProjectDirectory = $projectDirectory; ComposeProject = $ComposeProject
        Tokens = @{}; Suffix = [guid]::NewGuid().ToString("N").Substring(0, 8).ToUpperInvariant()
    }
    $state = Get-DemoState
    $null = Invoke-MarketplaceRequest -Path "/health"
    if ($null -eq $Password) { $Password = Read-Host "Password used in scripts/seed.ps1" -AsSecureString }
    $pointer = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($Password)
    try {
        $plainPassword = [System.Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
        foreach ($role in @("Seller", "Admin")) {
            $pair = Invoke-MarketplaceRequest -Method POST -Path "/auth/login" -Body @{
                username = "demo_" + $role.ToLowerInvariant(); password = $plainPassword
            } -Quiet
            $state.Tokens[$role] = $pair.Data.access_token
            Write-Host "Logged in as demo_$($role.ToLowerInvariant()); token kept in memory."
        }
    }
    finally {
        [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
        $plainPassword = $null
    }
    $state.Buyer = New-DemoBuyer
    $state.Tokens.User = $state.Buyer.Access
    Write-Host "Fresh USER for this run: $($state.Buyer.Username)."
    return
}

$state = Get-DemoState
if ($Step -eq "PromoFixtures") { $Step = "Promos" } # Keep the old command usable.
switch ($Step) {
    "Products" {
        $state.Category = "DEMO_" + $state.Suffix
        $first = Invoke-MarketplaceRequest -Method POST -Path "/products" -Token $state.Tokens.Seller -ExpectedStatus 201 -Body @{
            name = "Demo keyboard"; price = 100; stock = 10; category = $state.Category; status = "ACTIVE"
        }
        $second = Invoke-MarketplaceRequest -Method POST -Path "/products" -Token $state.Tokens.Seller -ExpectedStatus 201 -Body @{
            name = "Demo mouse"; price = 25; stock = 10; category = $state.Category; status = "ACTIVE"
        }
        $state.ProductId, $state.SecondProductId = $first.Data.id, $second.Data.id
        $null = Invoke-MarketplaceRequest -Path "/products/$($state.ProductId)" -Token $state.Tokens.User
        foreach ($page in @(0, 1)) {
            $list = Invoke-MarketplaceRequest -Path "/products?status=ACTIVE&category=$($state.Category)&page=$page&size=1" -Token $state.Tokens.User
            Assert-Demo ($list.Data.totalElements -eq 2 -and @($list.Data.items).Count -eq 1) "Pagination/filter mismatch."
        }
        $null = Invoke-MarketplaceRequest -Method PUT -Path "/products/$($state.ProductId)" -Token $state.Tokens.Seller -Body @{
            name = "Demo keyboard updated"; price = 120; stock = 10; category = $state.Category; status = "ACTIVE"
        }
        $archived = Invoke-MarketplaceRequest -Method POST -Path "/products" -Token $state.Tokens.Seller -ExpectedStatus 201 -Body @{
            name = "Demo archived product"; price = 5; stock = 1; category = $state.Category; status = "ACTIVE"
        }
        $state.ArchivedId = $archived.Data.id
        $null = Invoke-MarketplaceRequest -Method DELETE -Path "/products/$($state.ArchivedId)" -Token $state.Tokens.Seller -ExpectedStatus 204
        $read = Invoke-MarketplaceRequest -Path "/products/$($state.ArchivedId)" -Token $state.Tokens.User
        Assert-Demo ($read.Data.status -eq "ARCHIVED") "Product was not archived."
        $saved = Invoke-DemoSql -Scalar -Sql "SELECT status FROM products WHERE id = :'id'::uuid;" -Variables @{ id = $state.ArchivedId }
        Assert-Demo ($saved -eq "ARCHIVED") "Archived row is absent from PostgreSQL."
        $null = Invoke-MarketplaceRequest -Method POST -Path "/products" -Token $state.Tokens.Seller -ExpectedStatus 400 -ExpectedError "VALIDATION_ERROR" -Body @{
            name = ""; price = 0; stock = -1; category = $state.Category; status = "ACTIVE"
        }
    }
    "Promos" {
        $state.PercentCode = "DEMO_P_" + $state.Suffix
        $state.FixedCode = "DEMO_F_" + $state.Suffix
        $from = [DateTimeOffset]::UtcNow.AddDays(-1).ToString("o")
        $until = [DateTimeOffset]::UtcNow.AddDays(30).ToString("o")
        $percentBody = @{ code = $state.PercentCode; discount_type = "PERCENTAGE"; discount_value = 10
                         min_order_amount = 200; max_uses = 100; valid_from = $from; valid_until = $until }
        $fixedBody = @{ code = $state.FixedCode; discount_type = "FIXED_AMOUNT"; discount_value = 15
                       min_order_amount = 0; max_uses = 100; valid_from = $from; valid_until = $until }
        $percent = Invoke-MarketplaceRequest -Method POST -Path "/promo-codes" -Token $state.Tokens.Seller -Body $percentBody -ExpectedStatus 201
        $fixed = Invoke-MarketplaceRequest -Method POST -Path "/promo-codes" -Token $state.Tokens.Admin -Body $fixedBody -ExpectedStatus 201
        Assert-Demo ($percent.Data.current_uses -eq 0 -and $fixed.Data.current_uses -eq 0) "New promo counters must be zero."
        $null = Invoke-MarketplaceRequest -Method POST -Path "/promo-codes" -Token $state.Tokens.User -Body $percentBody -ExpectedStatus 403 -ExpectedError "ACCESS_DENIED"
        $null = Invoke-MarketplaceRequest -Method POST -Path "/promo-codes" -Token $state.Tokens.Admin -Body $percentBody -ExpectedStatus 409 -ExpectedError "PROMO_CODE_ALREADY_EXISTS"
        $invalidDates = $percentBody.Clone()
        $invalidDates.code = "BAD_" + $state.Suffix
        $invalidDates.valid_until = $from
        $null = Invoke-MarketplaceRequest -Method POST -Path "/promo-codes" -Token $state.Tokens.Seller -Body $invalidDates -ExpectedStatus 400 -ExpectedError "VALIDATION_ERROR"
    }
    "Order" {
        if (-not $state.ProductId -or -not $state.PercentCode) { throw "Run Products and Promos first." }
        $created = Invoke-MarketplaceRequest -Method POST -Path "/orders" -Token $state.Tokens.User -ExpectedStatus 201 -Body @{
            items = @(@{ product_id = $state.ProductId; quantity = 2 }); promo_code = $state.PercentCode
        }
        $state.OrderId = $created.Data.id
        Assert-Demo ($created.Data.total_amount -eq 216 -and $created.Data.discount_amount -eq 24) "Wrong order total."
        Assert-Demo ((Get-DemoProduct $state.ProductId).stock -eq 8) "Wrong reserved stock."
        Assert-Demo ((Get-DemoPromoUses $state.PercentCode) -eq 1) "Promo must be consumed once."
        $null = Invoke-MarketplaceRequest -Method PUT -Path "/products/$($state.ProductId)" -Token $state.Tokens.Seller -Body @{
            name = "Demo keyboard updated"; price = 130; stock = 8; category = $state.Category; status = "ACTIVE"
        }
        $read = Invoke-MarketplaceRequest -Path "/orders/$($state.OrderId)" -Token $state.Tokens.User
        Assert-Demo ($read.Data.total_amount -eq 216 -and $read.Data.items[0].price_at_order -eq 120) "Catalog change modified the snapshot."
    }
    "RateLimit" {
        if (-not $state.OrderId) { throw "Run Order first." }
        $interval = (Invoke-DemoCompose -Arguments @("exec", "-T", "api", "printenv", "ORDER_CREATE_INTERVAL_MINUTES") | Out-String).Trim()
        if ([double]::Parse($interval, [Globalization.CultureInfo]::InvariantCulture) -le 0) { throw "Set ORDER_CREATE_INTERVAL_MINUTES > 0 and recreate api to show 429." }
        $null = Invoke-MarketplaceRequest -Method POST -Path "/orders" -Token $state.Tokens.User -ExpectedStatus 429 -ExpectedError "ORDER_LIMIT_EXCEEDED" -Body @{
            items = @(@{ product_id = $state.ProductId; quantity = 1 })
        }
    }
    "ActiveOrder" {
        $state.ActiveBuyer = New-DemoBuyer
        $seeded = Invoke-DemoCompose -Arguments @("exec", "-T", "api", "python", "-m", "app.seed_orders", "--buyer", $state.ActiveBuyer.Username, "--seller", "demo_seller")
        $state.StateOrders = @{}
        foreach ($line in $seeded) {
            Write-Host $line
            if ($line -match '([0-9a-f-]{36}) \(([A-Z_]+)\)') { $state.StateOrders[$Matches[2]] = $Matches[1] }
        }
        Assert-Demo ($state.StateOrders.Count -eq 5) "Demo states were not seeded."
        $null = Invoke-MarketplaceRequest -Method POST -Path "/orders" -Token $state.ActiveBuyer.Access -ExpectedStatus 409 -ExpectedError "ORDER_HAS_ACTIVE" -Body @{
            items = @(@{ product_id = $state.ProductId; quantity = 1 })
        }
    }
    "Rollback" {
        $buyer = New-DemoBuyer
        $firstStock, $secondStock = (Get-DemoProduct $state.ProductId).stock, (Get-DemoProduct $state.SecondProductId).stock
        $null = Invoke-MarketplaceRequest -Method POST -Path "/orders" -Token $buyer.Access -ExpectedStatus 409 -ExpectedError "INSUFFICIENT_STOCK" -Body @{
            items = @(@{ product_id = $state.ProductId; quantity = 1 }, @{ product_id = $state.SecondProductId; quantity = 999 })
        }
        $null = Invoke-MarketplaceRequest -Method POST -Path "/orders" -Token $buyer.Access -ExpectedStatus 422 -ExpectedError "PROMO_CODE_INVALID" -Body @{
            items = @(@{ product_id = $state.ProductId; quantity = 1 }); promo_code = "MISSING_" + $state.Suffix
        }
        Assert-Demo ((Get-DemoProduct $state.ProductId).stock -eq $firstStock) "First stock changed after rollback."
        Assert-Demo ((Get-DemoProduct $state.SecondProductId).stock -eq $secondStock) "Second stock changed after rollback."
        $count = Invoke-DemoSql -Scalar -Sql "SELECT count(*) FROM user_operations WHERE user_id = :'id'::uuid;" -Variables @{ id = $buyer.Id }
        Assert-Demo ([int]$count -eq 0) "Failed requests wrote successful operations."
        $created = Invoke-MarketplaceRequest -Method POST -Path "/orders" -Token $buyer.Access -ExpectedStatus 201 -Body @{
            items = @(@{ product_id = $state.SecondProductId; quantity = 2 }); promo_code = $state.FixedCode
        }
        Assert-Demo ($created.Data.total_amount -eq 35 -and $created.Data.discount_amount -eq 15) "Wrong fixed discount."
        $null = Invoke-MarketplaceRequest -Method POST -Path "/orders/$($created.Data.id)/cancel" -Token $buyer.Access
        Assert-Demo ((Get-DemoProduct $state.SecondProductId).stock -eq $secondStock) "Cancel did not return stock."
        Assert-Demo ((Get-DemoPromoUses $state.FixedCode) -eq 0) "Cancel did not return the promo use."
    }
    "Update" {
        $updated = Invoke-MarketplaceRequest -Method PUT -Path "/orders/$($state.OrderId)" -Token $state.Tokens.User -Body @{
            items = @(@{ product_id = $state.ProductId; quantity = 1 }, @{ product_id = $state.SecondProductId; quantity = 1 })
        }
        Assert-Demo ($updated.Data.total_amount -eq 155 -and $null -eq $updated.Data.promo_code_id -and $updated.Data.discount_amount -eq 0) "Below-minimum promo was not removed."
        Assert-Demo ((Get-DemoPromoUses $state.PercentCode) -eq 0) "Removed promo use was not returned."
        Assert-Demo ((Get-DemoProduct $state.ProductId).stock -eq 9 -and (Get-DemoProduct $state.SecondProductId).stock -eq 9) "Wrong replacement stock."
    }
    "Cancel" {
        $null = Invoke-MarketplaceRequest -Method POST -Path "/orders/$($state.OrderId)/cancel" -Token $state.Tokens.User
        Assert-Demo ((Get-DemoProduct $state.ProductId).stock -eq 10 -and (Get-DemoProduct $state.SecondProductId).stock -eq 10) "Cancel did not restore stock."
        $null = Invoke-MarketplaceRequest -Method POST -Path "/orders/$($state.OrderId)/cancel" -Token $state.Tokens.User -ExpectedStatus 409 -ExpectedError "INVALID_STATE_TRANSITION"
        Assert-Demo ((Get-DemoProduct $state.ProductId).stock -eq 10) "Repeated cancel returned stock twice."
        Assert-Demo ((Get-DemoPromoUses $state.PercentCode) -eq 0) "Cancel returned a removed promo twice."
    }
    "Access" {
        $body = @{ name = "Admin product"; price = 1; stock = 1; category = $state.Category; status = "ACTIVE" }
        $null = Invoke-MarketplaceRequest -Method POST -Path "/products" -Token $state.Tokens.User -Body $body -ExpectedStatus 403 -ExpectedError "ACCESS_DENIED"
        $adminProduct = Invoke-MarketplaceRequest -Method POST -Path "/products" -Token $state.Tokens.Admin -Body $body -ExpectedStatus 201
        $null = Invoke-MarketplaceRequest -Method PUT -Path "/products/$($adminProduct.Data.id)" -Token $state.Tokens.Seller -Body $body -ExpectedStatus 403 -ExpectedError "ACCESS_DENIED"
        $other = New-DemoBuyer
        $null = Invoke-MarketplaceRequest -Path "/orders/$($state.OrderId)" -Token $other.Access -ExpectedStatus 403 -ExpectedError "ORDER_OWNERSHIP_VIOLATION"
        $null = Invoke-MarketplaceRequest -Path "/orders/$($state.OrderId)" -Token $state.Tokens.Seller -ExpectedStatus 403 -ExpectedError "ACCESS_DENIED"
        $null = Invoke-MarketplaceRequest -Path "/orders/$($state.OrderId)" -Token $state.Tokens.Admin
    }
    "Tokens" {
        $null = Invoke-MarketplaceRequest -Path "/products" -ExpectedStatus 401 -ExpectedError "TOKEN_INVALID"
        $null = Invoke-MarketplaceRequest -Path "/products" -Token "invalid-token" -ExpectedStatus 401 -ExpectedError "TOKEN_INVALID"
        $null = Invoke-MarketplaceRequest -Path "/products" -Token $state.Buyer.Refresh -ExpectedStatus 401 -ExpectedError "TOKEN_INVALID"
        $refreshed = Invoke-MarketplaceRequest -Method POST -Path "/auth/refresh" -Body @{ refresh_token = $state.Buyer.Refresh } -Quiet
        $state.Tokens.User = $refreshed.Data.access_token
        $null = Invoke-MarketplaceRequest -Path "/products?page=0&size=1" -Token $state.Tokens.User
    }
    "States" {
        if (-not $state.StateOrders) { throw "Run ActiveOrder first." }
        $paid = $state.StateOrders.PAID
        $null = Invoke-MarketplaceRequest -Method PUT -Path "/orders/$paid" -Token $state.ActiveBuyer.Access -ExpectedStatus 409 -ExpectedError "INVALID_STATE_TRANSITION" -Body @{
            items = @(@{ product_id = $state.ProductId; quantity = 1 })
        }
        $null = Invoke-MarketplaceRequest -Method POST -Path "/orders/$paid/cancel" -Token $state.ActiveBuyer.Access -ExpectedStatus 409 -ExpectedError "INVALID_STATE_TRANSITION"
        $null = Invoke-MarketplaceRequest -Method POST -Path "/orders/$($state.StateOrders.PAYMENT_PENDING)/cancel" -Token $state.ActiveBuyer.Access
    }
    "Inspect" { Invoke-DemoSql -Sql (Get-Content -LiteralPath (Join-Path $PSScriptRoot "inspect-db.sql") -Raw) }
    "Logs" { Invoke-DemoCompose -Arguments @("logs", "--tail", "40", "--no-log-prefix", "api") }
}
Write-Host "Step $Step passed."
