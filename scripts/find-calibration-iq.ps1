& {
    Write-Host "=== Listening ports (likely Calibration IQ services) ===" -ForegroundColor Cyan
    Get-NetTCPConnection -State Listen |
        Where-Object { $_.LocalPort -in 80,3000,3001,4000,5000,5173,8000,8001,8080,8081,8084,8088,9000 } |
        Select-Object LocalAddress, LocalPort, OwningProcess -Unique |
        ForEach-Object {
            $name = (Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue).ProcessName
            "{0,-10} {1,-6} pid {2,-7} {3}" -f $_.LocalAddress, $_.LocalPort, $_.OwningProcess, $name
        }

    Write-Host ""
    Write-Host "=== docker compose port mappings ===" -ForegroundColor Cyan
    $proj = "X:\calibration iq"
    if (Test-Path (Join-Path $proj "docker-compose.yml")) {
        Select-String -Path (Join-Path $proj "docker-compose.yml") -Pattern 'ports:|^\s+-\s+"?\d+:\d+|container_name|image:' -Context 0,0 |
            ForEach-Object { $_.Line.Trim() }
    } else { Write-Host "  no docker-compose.yml at $proj" }
    try { docker ps --format "{0}" -f "{{.Names}}  {{.Ports}}" 2>$null } catch {}

    Write-Host ""
    Write-Host "=== Probing for the tool API ===" -ForegroundColor Cyan
    $ports = 8084,8080,8000,8001,3000,5000,9000
    $paths = "/api/v1/tools/v1/calibration-iq/health","/api/v1/tools/health","/api/health","/health","/api/v1/tools/v1/calibration-iq/collection/ros"
    foreach ($p in $ports) {
        foreach ($path in $paths) {
            $url = "http://127.0.0.1:$p$path"
            try {
                $r = Invoke-WebRequest -Uri $url -TimeoutSec 2 -UseBasicParsing -ErrorAction Stop
                Write-Host ("  {0,-3} {1}" -f $r.StatusCode, $url) -ForegroundColor Green
            } catch {
                $code = $_.Exception.Response.StatusCode.value__
                if ($code) { Write-Host ("  {0,-3} {1}" -f $code, $url) -ForegroundColor Yellow }
            }
        }
    }
    Write-Host ""
    Write-Host "Green/yellow lines = something answered. 401 is GOOD (service up, needs token)." -ForegroundColor Cyan
}
