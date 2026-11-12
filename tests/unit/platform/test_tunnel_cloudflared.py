"""``src.tunnel.cloudflared`` — the named-tunnel facts the tray and the script share."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.tunnel import cloudflared


def test_read_hostname_takes_first_ingress_hostname(tmp_path) -> None:
    cfg = tmp_path / "cloudflared.yml"
    cfg.write_text(
        "ingress:\n  - service: http_status:404\n  - hostname: launcher.example.com\n"
        "  - hostname: other.example.com\n",
        encoding="utf-8",
    )
    assert cloudflared.read_hostname(cfg) == "launcher.example.com"


def test_read_hostname_is_none_when_missing_or_unparseable(tmp_path) -> None:
    assert cloudflared.read_hostname(tmp_path / "absent.yml") is None
    bad = tmp_path / "bad.yml"
    bad.write_text("ingress: [unclosed", encoding="utf-8")
    assert cloudflared.read_hostname(bad) is None


@pytest.fixture
def url_file(tmp_path, monkeypatch):
    path = tmp_path / "webapp" / "last_tunnel_url.txt"
    monkeypatch.setattr(cloudflared, "TUNNEL_URL_FILE", path)
    return path


def test_publish_appends_the_auth_token(url_file, monkeypatch) -> None:
    monkeypatch.setattr(
        cloudflared, "load_webapp_config", lambda: SimpleNamespace(auth_token=" tok ")
    )
    url = cloudflared.publish_tunnel_url("launcher.example.com")
    assert url is not None and url.startswith("https://launcher.example.com")
    assert "token=tok" in url
    assert url_file.read_text(encoding="utf-8") == url + "\n"


def test_publish_without_token_writes_the_bare_url(url_file, monkeypatch) -> None:
    monkeypatch.setattr(
        cloudflared, "load_webapp_config", lambda: SimpleNamespace(auth_token="")
    )
    assert cloudflared.publish_tunnel_url("h.example.com") == "https://h.example.com"
    assert url_file.read_text(encoding="utf-8") == "https://h.example.com\n"


def test_stop_terminates_the_child_and_removes_the_url_file(url_file) -> None:
    url_file.parent.mkdir(parents=True)
    url_file.write_text("https://h.example.com\n", encoding="utf-8")
    calls = []
    proc = SimpleNamespace(
        pid=1,
        send_signal=lambda sig: calls.append("signal"),
        terminate=lambda: calls.append("terminate"),
        wait=lambda timeout: calls.append("wait"),
        kill=lambda: calls.append("kill"),
    )
    cloudflared.stop_cloudflared(proc)
    assert "terminate" in calls and "kill" not in calls
    assert not url_file.exists()
