"""A config file the app cannot read must never be replaced with defaults.

A Windows editor can save ``webapp_config.json`` with a byte-order mark, which
a plain ``utf-8`` read rejects. The loader then answers with defaults, and a
save built on that answer would write them over the owner's values.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.webapp_config import (
    SESSION_HOST_PORT_ENV,
    WEBAPP_CONFIG_PATH_ENV,
    load_webapp_config,
    update_webapp_config,
)

OWNER = {"chief_hold_minutes": 25, "gitlab_group": "demo-group"}


@pytest.fixture
def config_file(tmp_path: Path, monkeypatch) -> Path:
    target = tmp_path / "webapp_config.json"
    monkeypatch.setenv(WEBAPP_CONFIG_PATH_ENV, str(target))
    monkeypatch.delenv(SESSION_HOST_PORT_ENV, raising=False)
    return target


def test_a_byte_order_mark_is_read_and_survives_a_save(config_file):
    config_file.write_text(json.dumps(OWNER), encoding="utf-8-sig")

    loaded = load_webapp_config()
    assert (loaded.chief_hold_minutes, loaded.gitlab_group) == (25, "demo-group")

    update_webapp_config(chief_reoffer_minutes=30)
    on_disk = json.loads(config_file.read_text(encoding="utf-8-sig"))
    assert on_disk["chief_reoffer_minutes"] == 30
    assert {key: on_disk[key] for key in OWNER} == OWNER


@pytest.mark.parametrize(
    "content",
    [b'{"gitlab_group": "demo-group",}', b'["demo-group"]'],
    ids=["bad-json", "not-an-object"],
)
def test_a_save_over_an_unreadable_file_is_refused(config_file, content):
    config_file.write_bytes(content)

    with pytest.raises(ValueError):
        update_webapp_config(chief_reoffer_minutes=30)

    assert config_file.read_bytes() == content


def test_a_refused_save_answers_with_the_reason(config_file, monkeypatch):
    """A route with no error handling of its own still tells the page why."""
    from app.webapp.server import app

    config_file.write_bytes(b"{broken")
    monkeypatch.setattr(app.state, "webapp_config", load_webapp_config())
    client = TestClient(app, client=("127.0.0.1", 50000))

    response = client.post("/api/claude-code/favorites", json={"id": "demo", "favorite": True})

    assert response.status_code == 409
    assert "nothing was saved" in response.json()["detail"]
    assert config_file.read_bytes() == b"{broken"
