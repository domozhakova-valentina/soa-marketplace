$ErrorActionPreference = "Stop"
$projectDirectory = Split-Path -Parent $PSScriptRoot
$environmentPath = Join-Path $projectDirectory ".env"
if (-not (Test-Path -LiteralPath $environmentPath)) {
    Copy-Item -LiteralPath (Join-Path $projectDirectory ".env.example") -Destination $environmentPath
}
$contents = [System.IO.File]::ReadAllText($environmentPath)
$keyPattern = '(?m)^JWT_SECRET=([^\r\n]*)\r?$'
$existing = [regex]::Match($contents, $keyPattern)
if ($existing.Success -and $existing.Groups[1].Value.Trim().Length -ge 32) {
    Write-Output "JWT_SECRET is already configured in hw2/.env."
    exit 0
}
$secretBytes = New-Object byte[] 48
$generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
try {
    $generator.GetBytes($secretBytes)
}
finally {
    $generator.Dispose()
}
$line = "JWT_SECRET=" + [Convert]::ToBase64String($secretBytes)
if ($existing.Success) {
    $contents = [regex]::Replace($contents, $keyPattern, $line)
}
else {
    $contents = $contents.TrimEnd() + "`r`n" + $line + "`r`n"
}
[System.IO.File]::WriteAllText($environmentPath, $contents, (New-Object System.Text.UTF8Encoding($false)))
Write-Output "JWT_SECRET saved in hw2/.env. The secret was not printed."
