"""The webapp's TLS certificate pair: where it lives and what it is.

One implementation for every consumer — the tray's webapp manager, the router
helpers' mirror-URL logic, ``scripts/gen_tailscale_cert.py`` and
``scripts/run_named_tunnel.py`` — so the lookup and the Tailscale discriminator
cannot drift apart.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def cert_dir(project_root: Optional[Path] = None) -> Path:
    return (project_root or PROJECT_ROOT) / "webapp" / "certificates"


def cert_paths(project_root: Optional[Path] = None) -> Optional[tuple[Path, Path]]:
    """``(cert.pem, key.pem)`` when both exist, else ``None`` (plain HTTP)."""
    directory = cert_dir(project_root)
    cert = directory / "cert.pem"
    key = directory / "key.pem"
    if cert.exists() and key.exists():
        return cert, key
    return None


def tailscale_hostname_from_cert(cert_path: Path) -> Optional[str]:
    """The ``.ts.net`` DNS SAN of a genuine ``tailscale cert`` leaf, else ``None``.

    Keyed on the ISSUER (Let's Encrypt), not the SAN alone: a legacy
    self-signed leaf (the retired gen_ssl_cert.py) also carried the ts.net
    name in its SAN, and a SAN-only test would treat it as renewable and route
    PC-mirror windows to a host it cannot serve. Any parse failure is ``None``.
    """
    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID

        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        issuer_orgs = [
            attr.value
            for attr in cert.issuer.get_attributes_for_oid(NameOID.ORGANIZATION_NAME)
        ]
        if not any("let's encrypt" in str(org).lower() for org in issuer_orgs):
            return None
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        for name in san.value.get_values_for_type(x509.DNSName):
            if ".ts.net" in name:
                return name
    except Exception:
        return None
    return None
