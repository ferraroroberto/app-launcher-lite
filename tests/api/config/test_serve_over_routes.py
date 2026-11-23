"""POST /api/config's serve_over fields and POST /api/settings/certificate."""

from __future__ import annotations

from app.webapp.routers import config as config_router


def test_serve_over_saves_and_reads_back(webapp_client):
    client, _app, _ = webapp_client
    assert client.get("/api/config").json()["serving"]["mode"] == "auto"
    assert client.post("/api/config", json={"serve_over": "both", "serve_http_port": 8470}).status_code == 200
    serving = client.get("/api/config").json()["serving"]
    assert (serving["mode"], serving["http_port"]) == ("both", 8470)
    assert client.post("/api/config", json={"serve_over": "ftp"}).status_code == 400
    assert client.post("/api/config", json={"serve_http_port": "80"}).status_code == 400


def test_serving_reports_a_failed_second_port(webapp_client, monkeypatch):
    from app.webapp import serve_modes
    client, _app, _ = webapp_client
    monkeypatch.setenv(serve_modes.HTTP_BIND_ENV, "failed:8475")
    assert client.get("/api/config").json()["serving"]["http_bind"] == {"bound": False, "port": 8475}
    monkeypatch.delenv(serve_modes.HTTP_BIND_ENV)
    assert client.get("/api/config").json()["serving"]["http_bind"] == {"bound": None, "port": None}


def test_the_certificate_is_created_once_and_never_replaced(webapp_client, tmp_path, monkeypatch):
    client, _app, _ = webapp_client
    cert_dir = tmp_path / "certificates"
    monkeypatch.setattr(config_router, "CERT_DIR", cert_dir)
    first = client.post("/api/settings/certificate")
    assert first.status_code == 200
    assert first.json()["serving"]["cert"]["self_signed"] is True
    before = (cert_dir / "cert.pem").read_bytes()
    assert client.post("/api/settings/certificate").status_code == 409
    assert (cert_dir / "cert.pem").read_bytes() == before
