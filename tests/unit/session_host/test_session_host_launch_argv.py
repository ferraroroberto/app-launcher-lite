"""A quoted launch token reaches the agent without literal quotes.

``PtyProcess.spawn`` tokenises a string command with
``shlex.split(posix=False)`` (quotes kept) and re-escapes them through
``subprocess.list2cmdline``, so the prompt and the session name arrived as
``\\"...\\"``. ``SessionManager._spawn_pty`` now hands it a pre-split argv.
"""

from __future__ import annotations

import asyncio
import subprocess

from src import session_host
from src.session_host import SessionManager
from src.session_hosting.launch_argv import pty_argv


def test_pty_argv_strips_one_quote_pair_per_token():
    argv = pty_argv('cmd /c copilot --name "My title" -i "/issue-start 9999"')

    assert argv == [
        "cmd", "/c", "copilot", "--name", "My title", "-i", "/issue-start 9999",
    ]


def test_pty_argv_leaves_unquoted_tokens_and_inner_quotes_alone():
    assert pty_argv("cmd /c copilot --model=gpt -i \"it's\"") == [
        "cmd", "/c", "copilot", "--model=gpt", "-i", "it's",
    ]


def test_spawn_pty_passes_bare_argv_and_a_single_quoted_command_line(
    tmp_path, monkeypatch
):
    captured: dict = {}

    class _FakePty:
        pid = 1

        @classmethod
        def spawn(cls, argv, **kwargs):
            captured["argv"] = argv
            raise RuntimeError("stop before the reader thread starts")

    monkeypatch.setattr(session_host, "PtyProcess", _FakePty)

    mgr = SessionManager()
    mgr._loop = asyncio.new_event_loop()
    try:
        try:
            mgr._spawn_pty(
                str(tmp_path),
                "proj",
                '--name "My title" -i "/issue-start 9999"',
                "copilot",
            )
        except RuntimeError:
            pass
    finally:
        mgr._loop.close()

    argv = captured["argv"]
    assert isinstance(argv, list)
    assert argv[-4:] == ["--name", "My title", "-i", "/issue-start 9999"]
    # What the agent's argv parser sees: one quote pair, no backslash-escaped
    # literal quotes riding inside the value.
    assert subprocess.list2cmdline(argv[1:]) == (
        '/c copilot --name "My title" -i "/issue-start 9999"'
    )
