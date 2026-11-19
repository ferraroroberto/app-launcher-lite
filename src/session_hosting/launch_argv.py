"""Build the argv a ConPTY spawn hands to ``PtyProcess.spawn``.

``PtyProcess.spawn`` tokenises a *string* command with
``shlex.split(..., posix=False)``, which keeps a token's surrounding quotes,
then rebuilds the command line with ``subprocess.list2cmdline`` — which
backslash-escapes those kept quotes. The agent then receives the quote
characters as part of the argument (``"/issue-start 9999"`` as the first
prompt). Tokenising here and stripping one matching pair per token gives
``list2cmdline`` the bare value, and it re-quotes a value containing a space
exactly once, the way ``cmd /c`` and the agent's own argv parser expect.
"""

from __future__ import annotations

import shlex
from typing import List

_QUOTES = ('"', "'")


def _strip_matching_quotes(token: str) -> str:
    """Drop one surrounding quote pair; a lone or mismatched quote stays."""
    if len(token) >= 2 and token[0] == token[-1] and token[0] in _QUOTES:
        return token[1:-1]
    return token


def pty_argv(command: str) -> List[str]:
    """Split ``command`` into argv tokens with each token's outer quotes removed."""
    return [_strip_matching_quotes(t) for t in shlex.split(command, posix=False)]
