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
        $statusJson = tailscale status --json | ConvertFrom-Json
    } catch {
        throw "Tailscale is not installed or not on PATH. Install it from https://tailscale.com/download and sign in."
    }

    $dnsName = $statusJson.Self.DNSName
    if (-not $dnsName) { throw "Could not read this machine's tailnet DNS name. Is Tailscale signed in?" }
    $host_ = $dnsName.TrimEnd('.')
    $origin = "https://$host_"

    Write-Host "Tailnet host : $host_"
    Write-Host ""

    # HTTPS certs must be enabled for the tailnet in the admin console
    # (DNS -> HTTPS Certificates). Without it, serve has no cert to use.
    Write-Host "Starting serve on 443 -> 127.0.0.1:8100 ..." -ForegroundColor Cyan
    Write-Host "(If this errors about certificates, enable HTTPS for your tailnet at" -ForegroundColor Yellow
    Write-Host " https://login.tailscale.com/admin/dns -> HTTPS Certificates, then re-run.)" -ForegroundColor Yellow
    Write-Host ""

    # CLI syntax has shifted between Tailscale releases; try the current
    # form first and fall back to the older one.
    try {
        tailscale serve --bg --https=443 http://127.0.0.1:8100
    } catch {
        Write-Host "Newer syntax failed, trying legacy form..." -ForegroundColor Yellow
        tailscale serve https:443 / http://127.0.0.1:8100
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
    Write-Host "Then restart Core. Stop serving with:  tailscale serve --https=443 off"
}
