from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_x_omni_serve_uses_its_own_https_port_without_replacing_calibration_iq():
    script = (ROOT / "scripts" / "tailscale-serve.ps1").read_text(encoding="utf-8")

    assert "$httpsPort = 8443" in script
    assert "--https=$httpsPort" in script
    assert "127.0.0.1:8100" in script
    assert "--https=443" not in script
    assert "Port 443 is left untouched for Calibration IQ." in script
    assert "$LASTEXITCODE" in script
    assert "serve status --json" in script
    assert "Tailscale Serve verification failed" in script
