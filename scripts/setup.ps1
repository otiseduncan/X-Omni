# X Omni -- one-time setup.
# Installs Python and Node dependencies and builds the UI.
#
#   cd "X:\X Omni"
#   .\scripts\setup.ps1

& {
    $ErrorActionPreference = "Stop"
    $root = Split-Path -Parent $PSScriptRoot

    function Assert-NativeSuccess {
        param([Parameter(Mandatory)][string]$Step)
        if ($LASTEXITCODE -ne 0) {
            throw "$Step failed with exit code $LASTEXITCODE."
        }
    }

    Write-Host "=== X Omni setup ===" -ForegroundColor Cyan
    Write-Host "Project root: $root"
    Write-Host ""

    # --- prerequisites ---
    Write-Host "--- Checking prerequisites ---" -ForegroundColor Cyan
    try {
        $bootstrapPython = (Get-Command python -ErrorAction Stop).Source
        $py = (& $bootstrapPython --version) 2>&1
        Write-Host "  Python : $py"
    } catch {
        throw "Python is not on PATH. Install Python 3.11+ and re-run."
    }
    try {
        $node = (node --version) 2>&1
        Write-Host "  Node   : $node"
    } catch {
        throw "Node.js is not on PATH. Install Node 18+ and re-run."
    }
    if (-not (Get-Command npm.cmd -ErrorAction SilentlyContinue)) {
        throw "npm.cmd is not on PATH. Repair the Node.js installation and re-run."
    }
    try {
        $null = (nvidia-smi --query-gpu=index --format=csv,noheader) 2>&1
        Write-Host "  nvidia-smi : found"
    } catch {
        Write-Host "  nvidia-smi : NOT FOUND -- model swapping cannot verify VRAM release." -ForegroundColor Yellow
    }
    Write-Host ""

    # --- isolated python runtime ---
    Write-Host "--- Preparing isolated Python runtime ---" -ForegroundColor Cyan
    $venvDir = Join-Path $root ".venv"
    $venvPython = Join-Path $venvDir "Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $venvPython)) {
        & $bootstrapPython -m venv $venvDir
        Assert-NativeSuccess -Step 'Python virtual environment creation'
        Write-Host "  Created .venv"
    } else {
        Write-Host "  Reusing .venv"
    }
    & $venvPython -m pip install --upgrade pip
    Assert-NativeSuccess -Step 'pip upgrade'
    & $venvPython -m pip install -r (Join-Path $root "requirements.lock.txt")
    Assert-NativeSuccess -Step 'Python dependency installation'
    Write-Host ""

    # --- env file ---
    $envLocal   = Join-Path $root "config\.env.local"
    $envExample = Join-Path $root "config\.env.example"
    if (-not (Test-Path $envLocal)) {
        Copy-Item $envExample $envLocal
        $secret = & $venvPython -c "import secrets; print(secrets.token_urlsafe(32))"
        (Get-Content $envLocal) `
            -replace '^XOMNI_SESSION_SECRET=$', "XOMNI_SESSION_SECRET=$secret" |
            Set-Content -Encoding UTF8 $envLocal
        Write-Host "Created config\.env.local with a generated session secret." -ForegroundColor Green
        Write-Host "Add your Google OAuth client ID and secret before enabling auth." -ForegroundColor Yellow
    } else {
        Write-Host "config\.env.local already exists -- leaving it alone." -ForegroundColor Green
    }
    Write-Host ""

    # --- model files ---
    Write-Host "--- Verifying model files ---" -ForegroundColor Cyan
    $workers = Get-Content (Join-Path $root "config\workers.json") -Raw | ConvertFrom-Json
    $allFound = $true
    foreach ($name in $workers.workers.PSObject.Properties.Name) {
        $w = $workers.workers.$name
        foreach ($pair in @(@("exe", $w.executable), @("model", $w.model_path))) {
            if (Test-Path $pair[1]) {
                Write-Host "  OK      $name $($pair[0])"
            } else {
                Write-Host "  MISSING $name $($pair[0]): $($pair[1])" -ForegroundColor Red
                $allFound = $false
            }
        }
        if ($w.mmproj) {
            if (Test-Path $w.mmproj) { Write-Host "  OK      $name mmproj" }
            else { Write-Host "  MISSING $name mmproj: $($w.mmproj)" -ForegroundColor Red; $allFound = $false }
        }
    }
    if (-not $allFound) {
        Write-Host "Fix the paths in config\workers.json before starting." -ForegroundColor Yellow
    }
    Write-Host ""

    # --- ui ---
    Write-Host "--- Building the UI (this takes a minute) ---" -ForegroundColor Cyan
    Push-Location (Join-Path $root "ui")
    try {
        & npm.cmd ci
        Assert-NativeSuccess -Step 'UI dependency installation'
        & npm.cmd run build
        Assert-NativeSuccess -Step 'UI production build'
    } finally {
        Pop-Location
    }

    $builtIndex = Join-Path $root "ui\dist\index.html"
    if (-not (Test-Path -LiteralPath $builtIndex)) {
        throw "UI build reported success but $builtIndex was not created."
    }

    Write-Host ""
    Write-Host "=== Setup complete ===" -ForegroundColor Green
    Write-Host "Start X Omni with:  .\scripts\start.ps1"
}
