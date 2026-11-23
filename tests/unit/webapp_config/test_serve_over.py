"""Settings > Serve over: the launch each mode builds, the certificate it can
create, and a Both start answering on both ports."""

from __future__ import annotations

import socket
import ssl
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from app.webapp import manager as manager_mod
from app.webapp.manager import WebappManager, WebappManagerConfig, cert_paths
from app.webapp.serve_modes import SERVE_MODES_MODULE, serve_plan
from src.auth import tls_certificate
from src.subprocess_flags import NO_WINDOW
from src.webapp_config import WebappConfig, _validate

CERTS = (Path("c.pem"), Path("k.pem"))


@pytest.mark.parametrize("mode, certs, scheme, http_port, warned", [
    ("auto", CERTS, "https", None, False),
    ("auto", None, "http", None, False),
    ("https", CERTS, "https", None, False),
    ("https", None, "http", None, True),
    ("http", CERTS, "http", None, False),
    ("both", CERTS, "https", 8467, False),
    ("both", None, "http", None, True),
])
def test_each_mode_plans_its_scheme_and_ports(mode, certs, scheme, http_port, warned):
    plan = serve_plan(mode, 8467, certs)
    assert (plan.scheme, plan.http_port, plan.warning is not None) == (scheme, http_port, warned)


def _command(monkeypatch, mode, certs):
    monkeypatch.setattr(manager_mod, "current_serve_plan", lambda: serve_plan(mode, 8467, certs))
    return WebappManager(WebappManagerConfig(port=8465))._build_command()


def test_the_launch_carries_each_modes_arguments(monkeypatch):
    https = _command(monkeypatch, "https", CERTS)
    assert https[1:3] == ["-m", "app.webapp.serve"] and "--ssl-certfile" in https
    assert "--ssl-certfile" not in _command(monkeypatch, "http", CERTS)
    both = _command(monkeypatch, "both", CERTS)
    assert both[1:3] == ["-m", SERVE_MODES_MODULE]
    assert both[both.index("--port") + 1] == "8465"
    assert both[both.index("--http-port") + 1] == "8467"
    assert both[both.index("--ssl-certfile") + 1] == "c.pem"


def test_the_default_setting_serves_as_before():
    assert WebappConfig().serve_over == "auto"
    assert serve_plan("auto", 8467, CERTS) == serve_plan("https", 8467, CERTS)


def test_both_refuses_an_http_port_the_app_already_uses():
    with pytest.raises(ValueError, match="serve_http_port"):
        _validate(WebappConfig(serve_over="both", serve_http_port=WebappConfig().port))


def test_a_created_pair_is_the_one_cert_paths_serves(tmp_path):
    cert_dir = tmp_path / "webapp" / "certificates"
    status = tls_certificate.create_self_signed(cert_dir, ["pc.example", "127.0.0.1"])
    assert status.present and status.self_signed and status.expires
    assert cert_paths(tmp_path) == (cert_dir / "cert.pem", cert_dir / "key.pem")


@pytest.mark.parametrize("existing", ["cert.pem", "key.pem"])
def test_an_existing_certificate_or_key_is_never_overwritten(tmp_path, existing):
    (tmp_path / existing).write_text("kept", encoding="utf-8")
    with pytest.raises(tls_certificate.CertificateExists):
        tls_certificate.create_self_signed(tmp_path, ["pc.example"])
    assert [p.name for p in tmp_path.iterdir()] == [existing]
    assert (tmp_path / existing).read_text(encoding="utf-8") == "kept"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _healthz(url: str, deadline: float) -> int:
    ctx = ssl.create_default_context()
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    while True:
        try:
            with urllib.request.urlopen(url, timeout=2, context=ctx) as resp:
                return resp.status
        except OSError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.2)


def test_a_both_start_answers_https_and_http(tmp_path):
    cert_dir = tmp_path / "certificates"
    tls_certificate.create_self_signed(cert_dir, ["127.0.0.1"])
    port, http_port = _free_port(), _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", SERVE_MODES_MODULE, "--http-port", str(http_port),
         "--host", "127.0.0.1", "--port", str(port), "--app", "tests.unit.webapp_config.test_serve_over:health_app",
         "--ssl-certfile", str(cert_dir / "cert.pem"), "--ssl-keyfile", str(cert_dir / "key.pem")],
        cwd=str(Path(__file__).resolve().parents[3]), creationflags=NO_WINDOW,
    )
    try:
        deadline = time.monotonic() + 20
        assert _healthz(f"https://127.0.0.1:{port}/healthz", deadline) == 200
        assert _healthz(f"http://127.0.0.1:{http_port}/healthz", deadline) == 200
    finally:
        proc.terminate()
        proc.wait(timeout=10)


async def health_app(scope, receive, send):
    """The smallest ASGI app that answers, so the Both test needs no webapp boot."""
    if scope["type"] == "lifespan":
        while (await receive())["type"] != "lifespan.shutdown":
            await send({"type": "lifespan.startup.complete"})
        await send({"type": "lifespan.shutdown.complete"})
        return
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})
