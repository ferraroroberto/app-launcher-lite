"""/api/team-mirror: links saved to the config only, the list's states, publish, the gate.

The repos are tests/_team_mirror_repos.py's throwaway pair; every name is invented.
"""

from __future__ import annotations

import pytest

from src.middleware.routes import terminal_guard_level
from src.team_mirror import publish
from src.webapp_config import update_webapp_config
from tests._team_mirror_repos import expected_files, files_at, make_world


@pytest.fixture
def mirror_env(webapp_client, tmp_path, monkeypatch):
    from src import middleware

    # The client stands in for the PC itself so the terminal gate steps aside;
    # the gate has its own test below.
    monkeypatch.setattr(middleware.trust, "LOOPBACK_HOSTS",
                        frozenset({"testclient", "127.0.0.1", "::1", "localhost"}))
    monkeypatch.setenv("APP_LAUNCHER_LITE_DATA_DIR", str(tmp_path / "data"))
    projects = tmp_path / "projects"
    world = make_world(tmp_path, monkeypatch, personal=projects / "shared-app")
    (projects / "other-app" / ".git").mkdir(parents=True)
    client, app, _ = webapp_client
    # Saved, not only set, because a link save reloads the config from its file.
    app.state.webapp_config = update_webapp_config(projects_dir=str(projects), team_mirrors=[])
    yield client, app, world
    for name in ("shared-app", "other-app"):
        publish.forget_status(name)


def _link(client, world, **over):
    body = {"team_url": str(world["team"]), "branch": "main", "leave_out": ["private", "local.cfg"], **over}
    return client.put("/api/team-mirror/links/shared-app", json=body)


def test_a_bad_link_is_refused_with_its_field(mirror_env):
    client, app, world = mirror_env
    resp = _link(client, world, branch="bad..name")
    assert resp.status_code == 400 and resp.json()["field"] == "branch"
    assert app.state.webapp_config.team_mirrors == []


def test_the_list_shows_linked_and_not_linked_apps(mirror_env):
    client, app, world = mirror_env
    assert _link(client, world).status_code == 200
    assert app.state.webapp_config.team_mirrors[0]["team_url"] == str(world["team"])

    body = client.get("/api/team-mirror").json()

    rows = {r["project"]: r for r in body["projects"]}
    assert rows["other-app"]["status"]["state"] == "not_linked"
    assert rows["shared-app"]["status"]["state"] == "behind" and rows["shared-app"]["host"] == "local"
    assert body["refresh_minutes"] == 30 and body["identity"]["name"] == "Work Robot"


def test_publish_through_the_api_mirrors_the_files(mirror_env):
    client, _, world = mirror_env
    _link(client, world)

    held = client.post("/api/team-mirror/shared-app/publish", json={}).json()
    assert held["result"] == "needs_confirmation"
    done = client.post("/api/team-mirror/shared-app/publish", json={"confirm_team_changes": True}).json()

    assert done["result"] == "merged"
    assert files_at(world["team"], "main") == expected_files(world["personal"])
    rows = {r["project"]: r for r in client.get("/api/team-mirror").json()["projects"]}
    assert rows["shared-app"]["status"]["state"] == "up_to_date"


def test_test_access_reports_a_wrong_url(mirror_env):
    client, _, world = mirror_env
    ok = client.post("/api/team-mirror/test-access", json={"team_url": str(world["team"])}).json()
    assert ok == {"ok": True, "error": None}
    bad = client.post("/api/team-mirror/test-access", json={"team_url": str(world["tmp"] / "nope.git")}).json()
    assert bad["ok"] is False and bad["error"].startswith("no repository at that URL")


def test_an_unlinked_or_unknown_app_is_404_and_a_bad_mode_400(mirror_env):
    client, _, world = mirror_env
    assert client.post("/api/team-mirror/shared-app/preview").status_code == 404
    assert client.put("/api/team-mirror/links/ghost", json={"team_url": str(world["team"])}).status_code == 404
    _link(client, world)
    assert client.post("/api/team-mirror/shared-app/publish", json={"mode": "force"}).status_code == 400


def test_every_route_but_the_list_is_passkey_gated():
    assert terminal_guard_level("/api/team-mirror") is None
    for path in ("/api/team-mirror/links/app", "/api/team-mirror/test-access",
                 "/api/team-mirror/app/preview", "/api/team-mirror/app/publish"):
        assert terminal_guard_level(path) == "passkey"
