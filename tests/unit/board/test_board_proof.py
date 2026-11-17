"""Done lane actions: the per-repo .proof store, the closing-PR link and GET /api/board/proof."""

from __future__ import annotations

import json
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.board import proof_reports
from src.clients import gitlab as forge

REPORT = "<!doctype html><html><body><h1>walkthrough</h1></body></html>"


@pytest.fixture
def _bypass_gate(monkeypatch):
    """The test client counts as the PC itself, as the outbox route tests do."""
    from src import middleware
    monkeypatch.setattr(
        middleware.trust, "LOOPBACK_HOSTS",
        frozenset({"testclient", "127.0.0.1", "::1", "localhost"}),
    )


def _write_proof(projects, repo, pr, body=REPORT):
    """One PR's report and index row, as ``ui_proof build`` writes them."""
    root = projects / repo / ".proof"
    (root / str(pr)).mkdir(parents=True, exist_ok=True)
    (root / str(pr) / "index.html").write_text(body, encoding="utf-8")
    index_path = root / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {}
    index[str(pr)] = {"title": "t", "branch": "b"}
    index_path.write_text(json.dumps(index), encoding="utf-8")
    return root


@pytest.fixture
def store(tmp_path, webapp_client):
    """The configured projects dir, holding demo-app#21's report in its own .proof."""
    projects = tmp_path / "projects"
    _write_proof(projects, "demo-app", 21)
    _client, app, _overrides = webapp_client
    app.state.webapp_config.projects_dir = str(projects)
    return str(projects)


def _card(number, pr=None, visual_proof=None, repo="demo-app"):
    return {"kind": "issue", "repo": repo, "number": number, "pr": pr, "visual_proof": visual_proof}


def test_a_stored_report_marks_its_issue_available(store):
    (card,) = proof_reports.annotate([_card(5, pr=21)], store)
    assert (card["proof"], card["proof_pr"]) == ("available", 21)
    assert "visual_proof" not in card


def test_a_pr_that_owes_proof_and_has_none_is_missing(store):
    for owed in ("missing", "stale", "failing", "unknown"):
        (card,) = proof_reports.annotate([_card(6, pr=22, visual_proof=owed)], store)
        assert (card["proof"], card["proof_pr"]) == ("missing", 22), owed


def test_an_index_miss_alone_says_nothing_about_a_non_ui_pr(store):
    for pr, line in ((23, None), (23, "fresh"), (23, "n/a"), (None, "missing")):
        (card,) = proof_reports.annotate([_card(7, pr=pr, visual_proof=line)], store)
        assert "proof" not in card, (pr, line)


def test_the_stored_report_wins_over_a_missing_line(store):
    (card,) = proof_reports.annotate([_card(5, pr=21, visual_proof="missing")], store)
    assert card["proof"] == "available"


def test_an_index_row_without_its_file_is_not_available(store):
    (Path(store) / "demo-app" / ".proof" / "21" / "index.html").unlink()
    assert proof_reports.report_path("demo-app", 21, store) is None


def test_no_store_at_all_is_an_empty_index(tmp_path):
    projects = str(tmp_path / "absent")
    assert proof_reports.report_path("demo-app", 21, projects) is None
    assert proof_reports.report_path("demo-app", 21, "") is None
    (card,) = proof_reports.annotate([_card(5, pr=21, visual_proof="missing")], projects)
    assert card["proof"] == "missing"


def test_the_lookup_is_jailed_to_the_repos_proof_folder(store, tmp_path):
    (tmp_path / "secret.html").write_text("x", encoding="utf-8")
    for repo in ("..", "../x", "a/b", "a\b", "", ".", "demo-app/../.."):
        assert proof_reports.report_path(repo, 21, store) is None, repo
    assert proof_reports.report_path("demo-app", True, store) is None
    assert proof_reports.report_path("demo-app", -1, store) is None


def test_a_row_for_another_pr_never_serves_this_ones_page(store):
    root = Path(store) / "demo-app" / ".proof"
    index = json.loads((root / "index.json").read_text(encoding="utf-8"))
    index["30"] = {}
    (root / "index.json").write_text(json.dumps(index), encoding="utf-8")
    assert proof_reports.report_path("demo-app", 30, store) is None   # no 30/index.html


def test_a_page_that_resolves_outside_the_proof_folder_is_refused(store, tmp_path):
    root = Path(store) / "demo-app" / ".proof"
    outside = tmp_path / "elsewhere" / "40"   # named like the PR, so only the jail refuses it
    outside.mkdir(parents=True)
    (outside / "index.html").write_text("secret", encoding="utf-8")
    link = root / "40"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        import _winapi  # a junction needs no privilege; this branch only runs on Windows
        _winapi.CreateJunction(str(outside), str(link))
    index = json.loads((root / "index.json").read_text(encoding="utf-8"))
    index["40"] = {}
    (root / "index.json").write_text(json.dumps(index), encoding="utf-8")
    assert proof_reports.report_path("demo-app", 40, store) is None


def test_an_oversized_report_is_refused(store, monkeypatch):
    monkeypatch.setattr(proof_reports, "MAX_REPORT_BYTES", 5)
    assert proof_reports.report_path("demo-app", 21, store) is None


def test_the_index_is_reread_when_it_changes(store):
    assert proof_reports.report_path("demo-app", 22, store) is None
    _write_proof(Path(store), "demo-app", 22)
    assert proof_reports.report_path("demo-app", 22, store) is not None


def test_each_repo_reads_its_own_store(store):
    _write_proof(Path(store), "other-app", 21, body="<p>other</p>")
    mine = proof_reports.report_path("demo-app", 21, store)
    theirs = proof_reports.report_path("other-app", 21, store)
    assert mine != theirs and theirs.read_text(encoding="utf-8") == "<p>other</p>"


# --- the closing-MR link ----------------------------------------------------

def _mr(number, body, merged_at, repo="demo-app"):
    return {"iid": number, "description": body, "merged_at": merged_at,
            "web_url": f"https://gitlab.com/o/{repo}/-/merge_requests/{number}",
            "references": {"full": f"o/{repo}!{number}"}}


def _issue(number, closed_at, repo="demo-app"):
    return {"iid": number, "title": "t", "web_url": "u", "updated_at": closed_at,
            "closed_at": closed_at, "labels": [], "references": {"full": f"o/{repo}#{number}"}}


def _glab_answering(monkeypatch, *, issues, mrs):
    def fake_run(argv, **kwargs):
        path = argv[argv.index("api") + 1]
        rows = mrs if "/merge_requests?" in path else issues
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(rows), stderr="")

    monkeypatch.setattr(forge.subprocess, "run", fake_run)


def test_the_report_is_served_with_a_scriptless_policy(webapp_client, _bypass_gate, store):
    client, _app, _overrides = webapp_client
    res = client.get("/api/board/proof/demo-app/21")
    assert res.status_code == 200 and res.text == REPORT
    assert res.headers["content-type"].startswith("text/html")
    assert res.headers["cache-control"] == "no-store"
    assert res.headers["x-content-type-options"] == "nosniff"
    csp = res.headers["content-security-policy"]
    assert csp.startswith("sandbox;") and "default-src 'none'" in csp


def test_a_missing_report_is_a_404(webapp_client, _bypass_gate, store):
    client, _app, _overrides = webapp_client
    assert client.get("/api/board/proof/demo-app/99").status_code == 404
    assert client.get("/api/board/proof/..%2F/21").status_code == 404
    assert client.get("/api/board/proof/demo-app/notanumber").status_code == 422


def test_off_the_tailnet_the_report_is_not_reachable(webapp_client, store):
    client, _app, _overrides = webapp_client
    assert client.get("/api/board/proof/demo-app/21").status_code == 403


def test_the_report_route_is_a_passkey_route():
    from src import middleware
    assert middleware._terminal_guard_level("/api/board/proof/demo-app/21") == "passkey"


def test_api_done_annotates_the_days_cards(webapp_client, store, monkeypatch):
    client, app, _overrides = webapp_client
    app.state.webapp_config.gitlab_group = "o"
    day = date.today() - timedelta(days=2)
    noon = datetime.combine(day, datetime.min.time()).astimezone().replace(hour=12)
    stamp = noon.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    _glab_answering(monkeypatch, issues=[_issue(5, stamp), _issue(6, stamp), _issue(7, stamp)], mrs=[
        _mr(21, "Closes #5", stamp), _mr(22, "Closes #6\nVISUAL_PROOF=missing", stamp),
        _mr(23, "Closes #7", stamp)])
    items = client.get("/api/board/done?days_back=2").json()["items"]
    assert {i["number"]: i.get("proof") for i in items} == {5: "available", 6: "missing", 7: None}
    assert {i["number"]: i.get("proof_pr") for i in items}[5] == 21
