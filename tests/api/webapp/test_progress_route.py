"""GET /api/progress: the route answers from the cache and refuses a bad bucket or range."""

from __future__ import annotations

from src.clients import gitlab as gitlab_client
from src.progress import totals


def _forbid_reads(monkeypatch):
    def refused(*_a, **_k):
        raise AssertionError("a read of the cache must not run git or glab")

    monkeypatch.setattr(totals, "read_git", refused)
    monkeypatch.setattr(gitlab_client, "project_history", refused)
    monkeypatch.setattr(totals, "start_refresh", refused)


def test_answers_from_the_cache_without_any_forge_call(webapp_client, tmp_path, monkeypatch):
    client, _, _ = webapp_client
    monkeypatch.setattr(totals, "TOTALS_FILE", tmp_path / "never.json")
    _forbid_reads(monkeypatch)
    body = client.get("/api/progress?bucket=week&from=2026-11-01&to=2026-11-14").json()
    assert body["bucket"] == "week" and body["from"] == "2026-11-01"
    assert body["git"]["state"] == "unknown" and body["gitlab"]["state"] == "unknown"
    assert [b["start"] for b in body["buckets"]] == ["2026-10-26", "2026-11-02", "2026-11-09"]
    assert all(b["git"] is None for b in body["buckets"])


def test_refresh_true_starts_one_background_refresh(webapp_client, tmp_path, monkeypatch):
    client, _, _ = webapp_client
    monkeypatch.setattr(totals, "TOTALS_FILE", tmp_path / "never.json")
    calls = []
    monkeypatch.setattr(totals, "start_refresh", lambda *a, **k: calls.append(a) or True)
    body = client.get("/api/progress?refresh=true").json()
    assert len(calls) == 1 and body["refreshing"] is True


def test_a_bad_bucket_or_range_is_a_422_with_a_sentence(webapp_client):
    client, _, _ = webapp_client
    assert client.get("/api/progress?bucket=year").status_code == 422
    bad = client.get("/api/progress?from=2026-11-05&to=2026-11-01")
    assert bad.status_code == 422 and "from must not be after to" in bad.json()["detail"]
    assert client.get("/api/progress?from=nope").status_code == 422
