"""Provision a Tailscale HTTPS certificate for this machine.

The app's only HTTPS provisioner (issue #383 retired the self-signed-CA
generator): browsers trust https://<machine>.tail*.ts.net:8465 without any
manual certificate installation on any device — ``tailscale cert`` issues a
real Let's Encrypt leaf for the tailnet MagicDNS name, so there is no CA
install, no mobileconfig detour, no Certificate Trust toggle.
Fleet standard: ferraroroberto/project-scaffolding#89.

Prerequisites:
  1. Enable HTTPS in the Tailscale admin console:
     https://login.tailscale.com/admin/dns  (scroll to "HTTPS Certificates")
  2. tailscale must be running and authenticated on this machine.

Usage:
    # Provision or force-renew:
    & .venv/Scripts/python.exe scripts/gen_tailscale_cert.py
    & .venv/Scripts/python.exe scripts/gen_tailscale_cert.py <host>.ts.net

    # Check and auto-renew if expiring within 30 days (called on startup by
    # webapp.bat, the tray's webapp manager, and run_named_tunnel.py):
    & .venv/Scripts/python.exe scripts/gen_tailscale_cert.py --check
"""
from __future__ import annotations

import datetime
import json
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.subprocess_flags import NO_WINDOW  # noqa: E402
from src.tls.certs import tailscale_hostname_from_cert  # noqa: E402

CERT_DIR = PROJECT_ROOT / "webapp" / "certificates"
RENEW_WITHIN_DAYS = 30


def _tailscale_hostname() -> str:
    result = subprocess.run(
        ["tailscale", "status", "--json"],
        capture_output=True,
        text=True,
        creationflags=NO_WINDOW,
    )
    if result.returncode != 0:
        raise SystemExit("tailscale status failed. Is tailscale running?")
    data = json.loads(result.stdout)
    name = data.get("Self", {}).get("DNSName", "").rstrip(".")
    if not name:
        raise SystemExit("Could not detect Tailscale hostname from 'tailscale status'.")
    return name


def _expiring_within(cert_path: Path, days: int) -> bool:
    """Return True if the cert expires within `days` days."""
    try:
        from cryptography import x509
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        try:
            expiry = cert.not_valid_after_utc
        except AttributeError:  # cryptography < 42
            expiry = cert.not_valid_after.replace(tzinfo=datetime.timezone.utc)
        threshold = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=days)
        return expiry < threshold
    except Exception:
        return False


def _provision(hostname: str) -> None:
    CERT_DIR.mkdir(parents=True, exist_ok=True)
    cert_path = CERT_DIR / "cert.pem"
    key_path = CERT_DIR / "key.pem"

    result = subprocess.run(
        [
            "tailscale", "cert",
            "--cert-file", str(cert_path),
            "--key-file", str(key_path),
            hostname,
        ],
        capture_output=True,
        text=True,
        creationflags=NO_WINDOW,
    )
    if result.returncode != 0:
        msg = (result.stderr or result.stdout).strip()
        print(msg)
        raise SystemExit(
            "\ntailscale cert failed.\n"
            "Make sure HTTPS certificates are enabled in the Tailscale admin console:\n"
            "  https://login.tailscale.com/admin/dns"
        )

    print(f"[OK] cert.pem -> {cert_path}")
    print(f"[OK] key.pem  -> {key_path}")


def _check_and_renew() -> None:
    """Renew the cert if it is a Tailscale cert expiring within RENEW_WITHIN_DAYS days.
    Always exits cleanly — startup must not be blocked by cert errors."""
    cert_path = CERT_DIR / "cert.pem"
    if not cert_path.exists():
        return

    hostname = tailscale_hostname_from_cert(cert_path)
    if hostname is None:
        return  # self-signed cert; leave it alone

    if not _expiring_within(cert_path, RENEW_WITHIN_DAYS):
        return

    print(f"[INFO] Tailscale cert for {hostname} expires within {RENEW_WITHIN_DAYS} days — renewing.")
    if shutil.which("tailscale") is None:
        print("[WARN] tailscale not found on PATH; skipping cert renewal.")
        return
    try:
        _provision(hostname)
        print("[OK] Tailscale cert renewed.")
    except SystemExit as exc:
        print(f"[WARN] Cert renewal failed: {exc}")


def main() -> None:
    args = sys.argv[1:]

    if args and args[0] == "--check":
        _check_and_renew()
        return

    if shutil.which("tailscale") is None:
        raise SystemExit("tailscale not found on PATH.")

    hostname = args[0] if args else _tailscale_hostname()
    print(f"Provisioning Tailscale cert for: {hostname}")
    _provision(hostname)
    print()
    print("Restart the webapp (tray.bat --restart or webapp.bat), then open:")
    print(f"  https://{hostname}:8465")
    print()
    print("Note: the cert is issued only for the Tailscale hostname, so")
    print("https://127.0.0.1:8465 and LAN-IP URLs will show a hostname-mismatch")
    print("warning. The PC mirror windows connect over loopback — see the")
    print("README HTTPS section before switching an instance that uses them.")


if __name__ == "__main__":
    main()
