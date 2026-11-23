"""Create a self-signed certificate for this PC, and describe the pair on disk.

A phone's browser only opens the microphone on an HTTPS page. Settings >
Connections > Serve over creates a pair here when HTTPS is chosen and
``webapp/certificates/`` holds none, so recording works without Tailscale.

It is a self-signed leaf, not a local CA plus a leaf: a CA is only worth
having when the phone installs it, and a locked-down phone cannot. Chrome
still treats the page as a secure origin, microphone included, once the user
taps Proceed on its warning. What an untrusted certificate still blocks
(install as app, the service worker, passkeys) is said in the Settings help.

A pair that exists is never overwritten, whoever made it: a Tailscale-issued
certificate keeps working as before.
"""

from __future__ import annotations

import datetime
import ipaddress
import logging
import os
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from src.subprocess_flags import NO_WINDOW

logger = logging.getLogger(__name__)

CERT_NAME = "cert.pem"
KEY_NAME = "key.pem"
# Under the 398 days Chrome allows a publicly trusted leaf, so an expiry
# check written for real certificates never treats this one as odd.
VALID_DAYS = 397
ICACLS_TIMEOUT_S = 15
COMMON_NAME = "App Launcher Lite"


class CertificateExists(Exception):
    """``cert.pem`` or ``key.pem`` is already there; nothing was written."""


@dataclass(frozen=True)
class CertificateStatus:
    present: bool
    # None when no pair is present or the certificate cannot be parsed.
    self_signed: Optional[bool] = None
    expires: Optional[str] = None


def machine_names(extra: Iterable[str] = ()) -> List[str]:
    """The names and addresses this PC answers on: hostname, localhost, loopback and ``extra``.

    ``extra`` carries what the caller already knows (the LAN address, the
    tailnet name); blanks and duplicates are dropped, order is kept.
    """
    names = [socket.gethostname(), socket.getfqdn(), "localhost", "127.0.0.1", *extra]
    seen: List[str] = []
    for name in names:
        name = (name or "").strip()
        if name and name.lower() not in (n.lower() for n in seen):
            seen.append(name)
    return seen


def _san(names: Iterable[str]) -> x509.SubjectAlternativeName:
    entries: List[x509.GeneralName] = []
    for name in names:
        try:
            entries.append(x509.IPAddress(ipaddress.ip_address(name)))
        except ValueError:
            entries.append(x509.DNSName(name))
    return x509.SubjectAlternativeName(entries)


def _restrict_to_user(path: Path) -> None:
    """Leave ``path`` readable by the current user only; best effort, logged when it fails."""
    if sys.platform != "win32":
        os.chmod(path, 0o600)
        return
    user = os.environ.get("USERNAME", "")
    if not user:
        logger.warning("⚠️ key permissions left inherited: USERNAME is not set")
        return
    try:
        result = subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
            capture_output=True, text=True, timeout=ICACLS_TIMEOUT_S, creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning(f"⚠️ key permissions left inherited: icacls did not run ({exc})")
        return
    if result.returncode != 0:
        logger.warning(f"⚠️ key permissions left inherited: icacls exited {result.returncode}")


def create_self_signed(cert_dir: Path, names: Iterable[str]) -> CertificateStatus:
    """Write a new self-signed ``cert.pem`` + ``key.pem`` into ``cert_dir`` for ``names``.

    Raises :class:`CertificateExists` when either file is already there; each
    file is opened exclusive-create, so a race cannot overwrite one either.
    """
    cert_path, key_path = cert_dir / CERT_NAME, cert_dir / KEY_NAME
    if cert_path.exists() or key_path.exists():
        raise CertificateExists(f"{cert_dir} already holds a certificate or key")
    names = list(names)
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, COMMON_NAME)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=VALID_DAYS))
        .add_extension(_san(names), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_dir.mkdir(parents=True, exist_ok=True)  # runtime-write-exempt: webapp/certificates/ (gitignored) is where cert_paths() and the Tailscale provisioner keep the pair
    key_bytes = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    with open(key_path, "xb") as fh:  # runtime-write-exempt: webapp/certificates/ (gitignored) is where cert_paths() and the Tailscale provisioner keep the pair
        fh.write(key_bytes)
    _restrict_to_user(key_path)
    try:
        with open(cert_path, "xb") as fh:  # runtime-write-exempt: webapp/certificates/ (gitignored) is where cert_paths() and the Tailscale provisioner keep the pair
            fh.write(cert.public_bytes(serialization.Encoding.PEM))
    except FileExistsError:
        # The key is ours from a moment ago; without its certificate it is useless.
        key_path.unlink()
        raise CertificateExists(f"{cert_dir} gained a certificate while this one was written") from None
    logger.info(f"✅ self-signed certificate created for {len(names)} name(s), valid {VALID_DAYS} days")
    return certificate_status(cert_dir)


def certificate_status(cert_dir: Path) -> CertificateStatus:
    """Whether a pair is present and, when it parses, whether it is self-signed and when it expires."""
    cert_path = cert_dir / CERT_NAME
    if not (cert_path.exists() and (cert_dir / KEY_NAME).exists()):
        return CertificateStatus(present=False)
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    except (OSError, ValueError) as exc:
        logger.warning(f"⚠️ {CERT_NAME} could not be read: {exc}")
        return CertificateStatus(present=True)
    return CertificateStatus(
        present=True,
        self_signed=cert.issuer == cert.subject,
        expires=cert.not_valid_after_utc.date().isoformat(),
    )
