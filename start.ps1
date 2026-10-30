# Start Outpost in Docker Desktop on Windows. Safe to run again: it rebuilds if
# needed, keeps all data, and only creates an admin the first time.
# Usually started by double-clicking start-windows.bat.
#
#   -DryRun   only work out the contest address and print it (used by CI)
#
# ASCII only on purpose: Windows PowerShell 5.1 misreads UTF-8 without a BOM.
param([switch]$DryRun)

Set-Location -Path $PSScriptRoot

function Find-ContestAddress {
    $all = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue)
    # Windows Mobile Hotspot always gives this laptop 192.168.137.1.
    $hotspot = $all | Where-Object { $_.IPAddress -like '192.168.137.*' } | Select-Object -First 1
    if ($hotspot) { return $hotspot.IPAddress }
    $lan = $all | Where-Object {
        $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' -and
        $_.InterfaceAlias -notmatch 'vEthernet|WSL|Docker|Loopback|Bluetooth'
    } | Select-Object -First 1
    if ($lan) { return $lan.IPAddress }
    return $null
}

$ip = Find-ContestAddress
if ($ip) {
    $env:OUTPOST_PUBLIC_URL = "http://${ip}:8000/"
    Write-Host "Contestants will open: $env:OUTPOST_PUBLIC_URL"
} else {
    Write-Host 'No network address found yet: turn the Mobile Hotspot on, or set the address on the admin dashboard.'
}

if ($DryRun) {
    Write-Host 'Dry run: Docker not started.'
    exit 0
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Host 'Docker Desktop is not installed: https://www.docker.com/products/docker-desktop/'
    exit 1
}
docker info *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host 'Docker Desktop is not running. Start it, wait for "Engine running", then run this again.'
    exit 1
}

# Let contestants' devices through Windows Firewall. Needs administrator
# rights once; if it fails, Docker Desktop's own rule usually covers it.
$ruleName = 'Outpost (TCP 8000)'
if (-not (Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue)) {
    try {
        New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Protocol TCP -LocalPort 8000 `
            -Action Allow -Profile Any -ErrorAction Stop | Out-Null
        Write-Host 'Opened port 8000 in Windows Firewall.'
    } catch {
        Write-Host 'Could not add a firewall rule for port 8000. If contestants cannot connect,'
        Write-Host 'run start-windows.bat once with right-click > Run as administrator.'
    }
}

docker compose up -d --build
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
docker compose exec -T outpost outpost admin jury --if-missing
Write-Host 'Checking compilers and the sandbox...'
docker compose exec -T outpost outpost selftest
if ($LASTEXITCODE -ne 0) { Write-Host '!! The self-test found problems (above). Fix them before a contest.' }

Write-Host ''
Write-Host 'Outpost is running.'
Write-Host '  Admin:       http://localhost:8000/admin   (user: jury; the password is printed above the first time)'
if ($ip) { Write-Host "  Contestants: $env:OUTPOST_PUBLIC_URL" }
Write-Host '  Stop:        docker compose down   (data is kept)'
Start-Process 'http://localhost:8000/admin'
