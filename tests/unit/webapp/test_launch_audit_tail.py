"""The post-spawn launch tail is one function every launch route shares.

``audit_session_start_and_maybe_mirror`` writes the ``session_start`` /
``remote_launch`` audit event and the per-session log entry, then mirrors the
session to a PC window. The Coding tab, the Board issue-start and the Team OS
skill launch all call it; the Team OS route must not carry its own copy. The
audit writes must hop off the single uvicorn loop (``audit_off_loop``), which
the first test pins by recording the thread each write runs on.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from typing import Any, List

from app.webapp.routers import _helpers


class _RecordingAudit:
    """Stand-in for ``src.audit``: records each write and the thread it ran on."""

    def __init__(self) -> None:
        self.calls: List[tuple] = []

    def audit_event(self, event: str, **fields: Any) -> None:
        self.calls.append(("audit_event", event, threading.get_ident(), fields))

    def session_log(self, sid: str, kind: str, **fields: Any) -> None:
        self.calls.append(("session_log", kind, threading.get_ident(), {"sid": sid, **fields}))


def _run_tail(audit: _RecordingAudit, **kwargs: Any) -> tuple[list, int]:
    """Drive the real helper on a fresh loop; return (mirror calls, loop thread id)."""
    mirrored: list = []
    cfg = SimpleNamespace(show_local_window=True)
    request = SimpleNamespace(client=SimpleNamespace(host="100.64.0.5"))

    async def go() -> int:
        await _helpers.audit_session_start_and_maybe_mirror(
            cfg,
            request,
            {},
            sid="s1",
            agent="copilot",
            name="proj",
            project="/p",
            audit_mod=audit,
            mirror_fn=lambda url, sid: mirrored.append((url, sid)),
            **kwargs,
        )
        # The mirror is fire-and-forget on a worker thread; let it land.
        await asyncio.sleep(0.05)
        return threading.get_ident()

    loop_thread = asyncio.run(go())
    return mirrored, loop_thread


def test_both_audit_writes_run_off_the_event_loop(monkeypatch) -> None:
    monkeypatch.setattr(_helpers, "mirror_url", lambda *a, **k: "http://mirror")
    audit = _RecordingAudit()
    _, loop_thread = _run_tail(audit)

    assert [c[0] for c in audit.calls] == ["audit_event", "session_log"]
    assert audit.calls[0][1] == "session_start"
    assert audit.calls[1][1] == "start"
    assert all(c[2] != loop_thread for c in audit.calls), (
        "audit writes must not run on the uvicorn loop thread"
    )


def test_event_name_and_mirror_flag_are_parameters(monkeypatch) -> None:
    monkeypatch.setattr(_helpers, "mirror_url", lambda *a, **k: "http://mirror")
    audit = _RecordingAudit()
    mirrored, _ = _run_tail(audit, event="remote_launch", mirror=False)

    assert audit.calls[0][1] == "remote_launch"
    assert mirrored == []  # a detached console has no PTY to mirror


def test_default_launch_mirrors_to_the_pc(monkeypatch) -> None:
    monkeypatch.setattr(_helpers, "mirror_url", lambda *a, **k: "http://mirror")
    mirrored, _ = _run_tail(_RecordingAudit())

    assert mirrored == [("http://mirror", "s1")]


# ------------------------------------------------------------- call sites
def _stub_team_os_spawn(monkeypatch, kind_seen: list) -> None:
    from app.webapp.routers import team_os as team_os_router

    def fake_spawn(project_dir, name, flags, port, kind, agent,
                   rows=40, cols=120, history_lines=None):
        kind_seen.append(kind)
        return {"session_id": "t1", "kind": kind}

    monkeypatch.setattr(team_os_router, "spawn_agent_session", fake_spawn)
    mirrored: list = []
    monkeypatch.setattr(
        team_os_router, "open_local_terminal_window",
        lambda *a, **k: mirrored.append(a),
    )
    return mirrored


def _team_os(webapp_client, tmp_path):
    client, app, overrides = webapp_client
    skill = tmp_path / "team-os" / ".claude" / "skills" / "journal-daily"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: journal-daily\ndescription: d\n---\n# journal-daily\n",
        encoding="utf-8",
    )
    app.state.webapp_config.team_os_dir = str(tmp_path / "team-os")
    return client, overrides["audit"]


def test_team_os_pty_launch_audits_through_the_shared_tail(
    webapp_client, tmp_path, monkeypatch
) -> None:
    client, audit = _team_os(webapp_client, tmp_path)
    _stub_team_os_spawn(monkeypatch, [])

    resp = client.post(
        "/api/team-os/skills/journal-daily/launch", json={"mode": "pty"}
    )

    assert resp.status_code == 200, resp.text
    events = [c.args[0] for c in audit.audit_event.call_args_list]
    assert "session_start" in events
    assert audit.session_log.call_args.args[:2] == ("t1", "start")


def test_team_os_remote_launch_audits_remote_launch_without_mirror(
    webapp_client, tmp_path, monkeypatch
) -> None:
    client, audit = _team_os(webapp_client, tmp_path)
    mirrored = _stub_team_os_spawn(monkeypatch, [])

    resp = client.post(
        "/api/team-os/skills/journal-daily/launch", json={"mode": "remote"}
    )

    assert resp.status_code == 200, resp.text
    events = [c.args[0] for c in audit.audit_event.call_args_list]
    assert "remote_launch" in events
    assert "session_start" not in events
    assert mirrored == []


def test_coding_launch_audits_session_start_through_the_shared_tail(
    webapp_client, monkeypatch
) -> None:
    from app.webapp.routers import apps as apps_router

    client, _, overrides = webapp_client
    audit = overrides["audit"]
    (overrides["tmp_projects_dir"] / "live-proj").mkdir()
    monkeypatch.setattr(apps_router.agents, "is_installed", lambda agent_id: True)
    monkeypatch.setattr(
        apps_router, "spawn_agent_session",
        lambda *a, **k: {"session_id": "c1", "kind": "pty"},
    )

    resp = client.post("/api/apps/live-proj/launch", json={})

    assert resp.status_code == 200, resp.text
    events = [c.args[0] for c in audit.audit_event.call_args_list]
    assert "session_start" in events
    assert audit.session_log.call_args.args[:2] == ("c1", "start")
