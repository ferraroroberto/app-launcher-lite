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
