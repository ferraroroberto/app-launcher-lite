"""Two throwaway local repos for the Team Mirror tests, no network.

A personal repo (a plain checkout with no remote) and a bare team repo seeded
with one team commit; git's global config and the personal-data term list
point into the same temp folder, so nothing on this computer is read. Every
name, address and file is invented.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

import pytest

from src.git_utils import run_git_proc

LEAVE_OUT = ("private", "local.cfg")
WORK_NAME, WORK_EMAIL = "Work Robot", "robot@team-host.test"
PERSONAL_NAME, PERSONAL_EMAIL = "Quillon Brask", "quillon@brask-home.test"
TERM = "Zorblatt"  # the term list's one private app name


def g(repo: Path, *args: str, author: tuple = (PERSONAL_NAME, PERSONAL_EMAIL)) -> str:
    proc = run_git_proc(repo, ["-c", f"user.name={author[0]}", "-c", f"user.email={author[1]}",
                               "-c", "commit.gpgSign=false", "-c", "core.autocrlf=false", *args], timeout=60)
    assert proc is not None and proc.returncode == 0, (args, proc and proc.stderr)
    return proc.stdout.strip()


def write(repo: Path, rel: str, data: bytes) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def commit_all(repo: Path, message: str, **kw) -> None:
    g(repo, "add", "-A", **kw)
    g(repo, "commit", "-q", "-m", message, **kw)


def files_at(repo: Path, ref: str) -> Dict[str, str]:
    """Every file of ``ref``'s tree with its mode and blob id: equal ids are equal bytes."""
    out = {}
    for line in filter(None, g(repo, "ls-tree", "-r", "-z", ref).split("\0")):
        meta, name = line.split("\t", 1)
        mode, _kind, blob = meta.split()
        out[name] = f"{mode} {blob}"
    return out


def make_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, personal: Optional[Path] = None) -> dict:
    """Two throwaway repos, git's global config and the term list, all under ``tmp_path``."""
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text(f"[user]\n\tname = {WORK_NAME}\n\temail = {WORK_EMAIL}\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    terms = tmp_path / "terms.toml"
    terms.write_text(f'names = []\nhandles = ["qbrask"]\nemails = []\ndomains = []\napps = ["{TERM}"]\n', encoding="utf-8")
    monkeypatch.setenv("PERSONAL_DATA_TERMS", str(terms))

    personal = personal or tmp_path / "personal"
    personal.mkdir(parents=True)
    g(personal, "init", "-q", "-b", "main")
    write(personal, "README.md", b"# Shared app\n")
    write(personal, "app/main.py", b"print('hi')\r\n")  # CRLF kept byte for byte
    write(personal, "assets/logo.bin", bytes(range(256)))
    write(personal, "private/notes.md", b"only for me\n")
    write(personal, "local.cfg", b"machine only\n")
    commit_all(personal, "feat: first cut")
    write(personal, "app/util.py", b"X = 1\n")
    commit_all(personal, "feat: add util")

    seed = tmp_path / "seed"
    seed.mkdir()
    g(seed, "init", "-q", "-b", "main")
    write(seed, "README.md", b"# Team copy\n")
    commit_all(seed, "Initial commit", author=("Team Lead", "lead@team-host.test"))
    team = tmp_path / "team.git"
    g(tmp_path, "clone", "-q", "--bare", str(seed), str(team))

    return {"personal": personal, "team": team, "seed": seed, "tmp": tmp_path}


def expected_files(personal: Path) -> Dict[str, str]:
    """The personal main's files minus the leave-out list :data:`LEAVE_OUT` names."""
    return {k: v for k, v in files_at(personal, "main").items()
            if not k.startswith("private/") and k != "local.cfg"}
