"""src.team_mirror end to end against two throwaway local repos (tests/_team_mirror_repos.py)."""

from __future__ import annotations

from pathlib import Path
from typing import List

import pytest

from src.team_mirror import forge, publish, snapshot
from src.team_mirror.links import LinkError, MirrorLink, checked_link, parse_links
from tests._team_mirror_repos import (
    LEAVE_OUT,
    PERSONAL_EMAIL,
    PERSONAL_NAME,
    TERM,
    WORK_EMAIL,
    WORK_NAME,
    commit_all,
    expected_files,
    files_at,
    g,
    make_world,
    write,
)


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    w = make_world(tmp_path, monkeypatch)
    link = MirrorLink("shared-app", str(w["team"]), "main", LEAVE_OUT)
    w["ctx"] = publish.Context("shared-app", w["personal"], link, scratch_root=tmp_path / "scratch",
                               state_path=tmp_path / "state.json")
    return w


def test_publish_and_merge_mirrors_the_tree_as_one_work_commit(world):
    team, personal, ctx = world["team"], world["personal"], world["ctx"]
    personal_refs = g(personal, "for-each-ref", "--format=%(refname) %(objectname)")
    before = int(g(team, "rev-list", "--count", "main"))

    first = publish.publish(ctx, confirm_team_changes=True)

    assert first["result"] == "merged"
    assert files_at(team, "main") == expected_files(personal)
    assert int(g(team, "rev-list", "--count", "main")) == before + 1
    head = g(team, "log", "-1", "--format=%an|%ae|%cn|%ce", "main")
    assert head == f"{WORK_NAME}|{WORK_EMAIL}|{WORK_NAME}|{WORK_EMAIL}"
    history = g(team, "log", "--all", "--format=%an %ae %cn %ce %B")
    assert PERSONAL_NAME not in history and PERSONAL_EMAIL not in history
    assert g(personal, "for-each-ref", "--format=%(refname) %(objectname)") == personal_refs
    assert publish.status(ctx)["state"] == "up_to_date"


def test_status_counts_commits_since_the_last_publish(world):
    personal, ctx = world["personal"], world["ctx"]
    first = publish.status(ctx)
    assert first["state"] == "behind" and first["behind"] is None and not first["published_before"]
    publish.publish(ctx, confirm_team_changes=True)
    write(personal, "app/util.py", b"X = 2\n")
    commit_all(personal, "fix: bump util")
    second = publish.status(ctx)
    assert (second["state"], second["behind"], second["team_changes"]) == ("behind", 1, 0)


def test_a_leave_out_only_change_is_up_to_date(world):
    personal, ctx = world["personal"], world["ctx"]
    publish.publish(ctx, confirm_team_changes=True)
    write(personal, "private/notes.md", b"still only for me\n")
    commit_all(personal, "chore: notes")
    assert publish.status(ctx)["state"] == "up_to_date"
    assert publish.publish(ctx)["result"] == "up_to_date"


def test_a_privacy_match_blocks_and_names_file_and_line(world):
    personal, team, ctx = world["personal"], world["team"], world["ctx"]
    write(personal, "docs/guide.md", f"one\ntwo\nsee {TERM} here\n".encode())
    commit_all(personal, "docs: guide")
    before = g(team, "rev-parse", "main")

    out = publish.publish(ctx, confirm_team_changes=True)

    assert out["result"] == "blocked"
    assert {"path": "docs/guide.md", "line": 3, "rule": "term-app"} in out["scan"]["findings"]
    assert TERM not in str(out)
    assert g(team, "rev-parse", "main") == before
    assert publish.status(ctx)["state"] == "blocked"


def test_an_unread_term_list_blocks_as_unknown(world, monkeypatch):
    monkeypatch.setenv("PERSONAL_DATA_TERMS", str(world["tmp"] / "missing.toml"))
    out = publish.publish(world["ctx"], confirm_team_changes=True)
    assert out["result"] == "blocked" and out["scan"]["state"] == "unknown"


def test_a_team_side_commit_needs_a_second_confirmation(world):
    team, seed, personal, ctx = world["team"], world["seed"], world["personal"], world["ctx"]
    publish.publish(ctx, confirm_team_changes=True)
    g(seed, "pull", "-q", str(team), "main")
    write(seed, "TEAM.md", b"a team note\n")
    commit_all(seed, "docs: team note", author=("Team Lead", "lead@team-host.test"))
    g(seed, "push", "-q", str(team), "main")
    write(personal, "app/util.py", b"X = 3\n")
    commit_all(personal, "fix: util again")

    held = publish.publish(ctx)
    assert held["result"] == "needs_confirmation"
    assert [c["subject"] for c in held["team_changes"]] == ["docs: team note"]

    assert publish.publish(ctx, confirm_team_changes=True)["result"] == "merged"
    assert files_at(team, "main") == expected_files(personal)


def test_first_publish_onto_a_seeded_team_repo_asks_first(world):
    out = publish.publish(world["ctx"])
    assert out["result"] == "needs_confirmation"
    assert [c["subject"] for c in out["team_changes"]] == ["Initial commit"]


def test_a_refused_merge_stops_at_an_open_pr(world, monkeypatch):
    team, ctx = world["team"], world["ctx"]
    before = g(team, "rev-parse", "main")
    calls: List[List[str]] = []

    def fake_run(argv, *, host=""):
        calls.append(list(argv))
        if argv[2] == "create":
            return "https://github.com/team-org/shared-app/pull/7\n"
        raise forge.ForgeError("gh pr merge: base branch policy prohibits the merge")

    monkeypatch.setattr(forge, "remote_of", lambda url: forge.Remote(forge.GITHUB, "github.com", "team-org/shared-app"))
    monkeypatch.setattr(forge, "_run", fake_run)

    out = publish.publish(ctx, confirm_team_changes=True)

    assert out["result"] == "pr_opened"
    assert out["url"] == "https://github.com/team-org/shared-app/pull/7"
    assert "did not allow merging" in out["note"]
    assert g(team, "rev-parse", "main") == before
    assert g(team, "rev-parse", "--verify", f"refs/heads/{out['branch']}")
    assert [c[:3] for c in calls] == [["gh", "pr", "create"], ["gh", "pr", "merge"]]


def test_open_pr_only_on_a_local_team_repo_pushes_the_branch(world):
    team, ctx = world["team"], world["ctx"]
    before = g(team, "rev-parse", "main")
    out = publish.publish(ctx, mode=publish.MODE_PR_ONLY, confirm_team_changes=True)
    assert out["result"] == "branch_pushed"
    assert g(team, "rev-parse", "main") == before
    assert files_at(team, out["branch"]) == expected_files(world["personal"])


def test_preview_lists_changes_and_the_drafted_text(world):
    out = publish.preview(world["ctx"])
    changes = {c["path"]: c["status"] for c in out["changes"]}
    assert changes["app/util.py"] == "A" and changes["README.md"] == "M"
    assert "private/notes.md" not in changes and "local.cfg" not in changes
    assert out["pr_body_source"] == "subjects" and "- feat: add util" in out["pr_body"]
    assert out["scan"]["state"] == "clear" and out["can_publish"] and out["needs_confirmation"]
    assert out["identity"] == {"name": WORK_NAME, "email": WORK_EMAIL}


def test_check_access_names_each_problem(world):
    repo = snapshot.scratch_repo("probe", world["tmp"] / "scratch")
    assert snapshot.check_access(repo, str(world["team"]), "main") is None
    assert "no branch named release" in snapshot.check_access(repo, str(world["team"]), "release")
    missing = snapshot.check_access(repo, str(world["tmp"] / "nowhere.git"), "main")
    assert missing.startswith("no repository at that URL")


def test_forge_is_chosen_from_the_url_host():
    assert forge.remote_of("https://github.com/team-org/app.git") == forge.Remote(forge.GITHUB, "github.com", "team-org/app")
    assert forge.remote_of("git@gitlab.team.test:grp/sub/app.git") == forge.Remote(forge.GITLAB, "gitlab.team.test", "grp/sub/app")
    assert forge.remote_of("file:///srv/team.git").kind == forge.LOCAL
    assert forge.remote_of("/srv/team.git").kind == forge.LOCAL


@pytest.mark.parametrize("row, field", [
    ({"project": "", "team_url": "https://github.com/o/r"}, "project"),
    ({"project": "a/b", "team_url": "https://github.com/o/r"}, "project"),
    ({"project": "app", "team_url": "not a url"}, "team_url"),
    ({"project": "app", "team_url": "https://github.com/o/r", "branch": "bad..name"}, "branch"),
    ({"project": "app", "team_url": "https://github.com/o/r", "leave_out": ["../up"]}, "leave_out"),
])
def test_checked_link_refuses_with_the_field(row, field):
    with pytest.raises(LinkError) as caught:
        checked_link(row)
    assert caught.value.field == field


def test_parse_links_drops_a_bad_row_only():
    links = parse_links([{"project": "app", "team_url": "https://github.com/o/r", "leave_out": ["a\\b/"]},
                         {"project": "", "team_url": "x"}, "junk"])
    assert list(links) == ["app"] and links["app"].leave_out == ("a/b",) and links["app"].branch == "main"
