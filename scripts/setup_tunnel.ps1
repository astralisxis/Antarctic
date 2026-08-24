$ErrorActionPreference = 'Stop'

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$TunnelName = 'dabbackwood-admin'
$Hostname = 'dabbackwood.cfd'
$LocalService = 'http://127.0.0.1:8080'
$ShopHostname = 'antarctic.cfd'
$ShopService = 'http://127.0.0.1:8081'
$Cloudflared = Join-Path $ProjectRoot 'tools\cloudflared.exe'

if (-not (Test-Path -LiteralPath $Cloudflared)) {
    $command = Get-Command cloudflared -ErrorAction SilentlyContinue
    if ($command) {
        $Cloudflared = $command.Source
    } else {
        Write-Error 'cloudflared not found. Put cloudflared-windows-amd64.exe in tools\cloudflared.exe.'
    }
}

$CloudflaredHome = Join-Path $ProjectRoot '.cloudflared'
$CertPath = Join-Path $env:USERPROFILE '.cloudflared\cert.pem'
if (-not (Test-Path -LiteralPath $CertPath)) {
    Write-Host 'Run Cloudflare login first:' -ForegroundColor Yellow
    Write-Host "  `"$Cloudflared`" tunnel login"
    Write-Host 'Select the dabbackwood.cfd zone, then run this script again.'
    exit 2
}

New-Item -ItemType Directory -Force -Path $CloudflaredHome | Out-Null
$Credentials = Join-Path $CloudflaredHome 'dabbackwood-admin.json'

$tunnels = @()
try {
    $json = (& $Cloudflared tunnel list --output json 2>$null | Out-String).Trim()
    if ($json) { $tunnels = @($json | ConvertFrom-Json) }
} catch {
    $tunnels = @()
}

$tunnel = $tunnels | Where-Object { $_.name -eq $TunnelName } | Select-Object -First 1
if (-not $tunnel) {
    Write-Host "Creating tunnel $TunnelName..."
    & $Cloudflared tunnel --origincert $CertPath create --credentials-file $Credentials $TunnelName
    if ($LASTEXITCODE -ne 0) { throw "Could not create Cloudflare Tunnel (exit code $LASTEXITCODE)." }
    $json = (& $Cloudflared tunnel list --output json 2>$null | Out-String).Trim()
    $tunnels = @($json | ConvertFrom-Json)
    $tunnel = $tunnels | Where-Object { $_.name -eq $TunnelName } | Select-Object -First 1
}

if (-not $tunnel -or -not $tunnel.id) {
    throw "Could not determine UUID for tunnel $TunnelName."
}

$TunnelId = [string]$tunnel.id
if (-not (Test-Path -LiteralPath $Credentials)) {
    throw "Credentials file not found: $Credentials"
}

$ConfigPath = Join-Path $CloudflaredHome 'dabbackwood-config.yml'
@"
tunnel: $TunnelId
credentials-file: $Credentials
ingress:
  - hostname: $Hostname
    service: $LocalService
  - hostname: $ShopHostname
    service: $ShopService
  - service: http_status:404
"@ | Set-Content -LiteralPath $ConfigPath -Encoding ascii

$DnsTarget = "${TunnelId}.cfargotunnel.com"
Write-Host "DNS target for both domains: $DnsTarget" -ForegroundColor Cyan
Write-Host "Cloudflare DNS must contain a proxied CNAME record: @ -> $DnsTarget"
Write-Host "Create it separately inside the $Hostname and $ShopHostname zones."
Write-Host "Do not create $ShopHostname inside the $Hostname zone."

Write-Host "Starting tunnel: https://$Hostname and https://$ShopHostname"
Write-Host 'Press Ctrl+C to stop.'
& $Cloudflared tunnel --protocol http2 --config $ConfigPath run $TunnelId
exit $LASTEXITCODE
