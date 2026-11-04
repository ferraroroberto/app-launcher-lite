"""``src.tls.certs`` — the single cert-pair lookup every consumer shares."""

from __future__ import annotations

from src.tls.certs import cert_paths


def test_cert_paths_needs_both_files(tmp_path) -> None:
    cert_dir = tmp_path / "webapp" / "certificates"
    cert_dir.mkdir(parents=True)
    assert cert_paths(tmp_path) is None
    (cert_dir / "cert.pem").write_text("c")
    assert cert_paths(tmp_path) is None  # key missing -> plain HTTP
    (cert_dir / "key.pem").write_text("k")
    assert cert_paths(tmp_path) == (cert_dir / "cert.pem", cert_dir / "key.pem")


def test_every_consumer_uses_the_one_implementation() -> None:
    """The tray manager, the router helpers and both scripts import the same
    functions; a local re-implementation would make one of these `is` checks fail."""
    from app.webapp import manager
    from app.webapp.routers import _helpers
    from scripts import gen_tailscale_cert, run_named_tunnel
    from src.tls import certs

    assert manager.cert_paths is certs.cert_paths
    assert _helpers.cert_paths is certs.cert_paths
    assert run_named_tunnel.cert_paths is certs.cert_paths
    assert _helpers.tailscale_hostname_from_cert is certs.tailscale_hostname_from_cert
    assert gen_tailscale_cert.tailscale_hostname_from_cert is certs.tailscale_hostname_from_cert


def test_tailscale_hostname_needs_lets_encrypt_issuer_and_tsnet_san(tmp_path) -> None:
    from src.tls.certs import tailscale_hostname_from_cert
    from tests.unit.webapp.test_router_helpers import _make_cert

    le = tmp_path / "le.pem"
    _make_cert(le, "Let's Encrypt", ["testhost.tailfake0.ts.net"])
    own = tmp_path / "own.pem"
    _make_cert(own, "Launcher", ["testhost.tailfake0.ts.net"])
    no_san = tmp_path / "no_san.pem"
    _make_cert(no_san, "Let's Encrypt", ["example.com"])

    assert tailscale_hostname_from_cert(le) == "testhost.tailfake0.ts.net"
    assert tailscale_hostname_from_cert(own) is None
    assert tailscale_hostname_from_cert(no_san) is None
    assert tailscale_hostname_from_cert(tmp_path / "missing.pem") is None
