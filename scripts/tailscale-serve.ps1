# X Omni -- expose Core to your tailnet over real HTTPS.
#
#   .\scripts\tailscale-serve.ps1
#
# Uses `tailscale serve` (tailnet-private), NEVER `tailscale funnel`
# (public internet). Funnel would put the operator core -- which can
# read files and run PowerShell on Omega -- behind nothing but a login
# form.
#
# HTTPS is not optional here: the browser microphone requires a secure
# context, so voice input silently fails over plain HTTP.

& {
    $ErrorActionPreference = "Stop"

    Write-Host "=== Tailscale serve for X Omni ===" -ForegroundColor Cyan

    try {
        $statusRaw = tailscale status --json
        if ($LASTEXITCODE -ne 0) {
            throw "tailscale status failed with exit code $LASTEXITCODE."
        }
        $statusJson = $statusRaw | ConvertFrom-Json
    } catch {
        throw "Tailscale is not installed or not on PATH. Install it from https://tailscale.com/download and sign in."
    }

    $dnsName = $statusJson.Self.DNSName
    if (-not $dnsName) { throw "Could not read this machine's tailnet DNS name. Is Tailscale signed in?" }
    $host_ = $dnsName.TrimEnd('.')
    # Port 443 is reserved for Calibration IQ on this shared Tailscale node.
    # A distinct HTTPS port gives X Omni its own browser origin without
    # rewriting Calibration IQ's existing Serve handler.
    $httpsPort = 8443
    $origin = "https://${host_}:$httpsPort"

    Write-Host "Tailnet host : $host_"
    Write-Host ""

    # HTTPS certs must be enabled for the tailnet in the admin console
    # (DNS -> HTTPS Certificates). Without it, serve has no cert to use.
    Write-Host "Starting X Omni on ${httpsPort} -> 127.0.0.1:8100 ..." -ForegroundColor Cyan
    Write-Host "Port 443 is left untouched for Calibration IQ." -ForegroundColor DarkGray
    Write-Host "(If this errors about certificates, enable HTTPS for your tailnet at" -ForegroundColor Yellow
    Write-Host " https://login.tailscale.com/admin/dns -> HTTPS Certificates, then re-run.)" -ForegroundColor Yellow
    Write-Host ""

    # CLI syntax has shifted between Tailscale releases; try the current
    # form first and fall back to the older one.
    try {
        tailscale serve --bg --https=$httpsPort http://127.0.0.1:8100
        if ($LASTEXITCODE -ne 0) {
            throw "tailscale serve failed with exit code $LASTEXITCODE."
        }
    } catch {
        Write-Host "Newer syntax failed, trying legacy form..." -ForegroundColor Yellow
        tailscale serve "https:$httpsPort" / http://127.0.0.1:8100
        if ($LASTEXITCODE -ne 0) {
            throw "Legacy tailscale serve failed with exit code $LASTEXITCODE."
        }
    }

    $serveStatusRaw = tailscale serve status --json
    if ($LASTEXITCODE -ne 0) {
        throw "Could not verify Tailscale Serve state (exit code $LASTEXITCODE)."
    }
    $serveStatus = $serveStatusRaw | ConvertFrom-Json
    $serveKey = "${host_}:$httpsPort"
    $serveEntry = $serveStatus.Web.PSObject.Properties[$serveKey].Value
    $proxy = if ($serveEntry) {
        $serveEntry.Handlers.PSObject.Properties['/'].Value.Proxy
    } else {
        $null
    }
    if ($proxy -ne 'http://127.0.0.1:8100') {
        throw "Tailscale Serve verification failed for X Omni HTTPS port $httpsPort."
    }

    Write-Host ""
    Write-Host "=== Serving ===" -ForegroundColor Green
    tailscale serve status
    Write-Host ""
    Write-Host "Reach X Omni from your phone at: $origin" -ForegroundColor Green
    Write-Host ""
    Write-Host "Two things to do now:" -ForegroundColor Cyan
    Write-Host "  1. Put this in config\.env.local :"
    Write-Host "         XOMNI_PUBLIC_ORIGIN=$origin"
    Write-Host "  2. Add this to your Google OAuth client's authorized redirect URIs:"
    Write-Host "         $origin/api/auth/callback"
    Write-Host ""
    Write-Host "Then restart Core. Stop only X Omni serving with:  tailscale serve --https=$httpsPort off"
}
