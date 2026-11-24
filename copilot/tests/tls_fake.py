"""A self-signed loopback certificate and a fake server that speaks TLS, for tests of the webapp's HTTPS mode."""

from __future__ import annotations

import datetime
import ipaddress
import ssl
from http.server import ThreadingHTTPServer
from pathlib import Path


def self_signed(folder: Path) -> ssl.SSLContext:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                       critical=False)
        .sign(key, hashes.SHA256())
    )
    (folder / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (folder / "key.pem").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(folder / "cert.pem", folder / "key.pem")
    return ctx


class TlsServer(ThreadingHTTPServer):
    """A plain `ThreadingHTTPServer` whose accepted sockets speak TLS, handshaking in the
    handler thread; a plaintext request then fails there and the connection drops."""

    tls: ssl.SSLContext

    def get_request(self):
        sock, addr = self.socket.accept()
        return self.tls.wrap_socket(sock, server_side=True, do_handshake_on_connect=False), addr

    def handle_error(self, request, client_address) -> None:
        pass  # a plaintext caller failing the handshake is the case under test
