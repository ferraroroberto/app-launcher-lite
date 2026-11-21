"""Force UTF-8 stdout/stderr under capture.

The global `CLAUDE.md` gotcha ("Windows Python: UTF-8 stdout under capture"):
piped/redirected stdout makes Python fall back to cp1252 on this machine, so
emoji/box-drawing `print()` throws `UnicodeEncodeError` and exits 1 — even
though the same script works fine in a real terminal. Every runtime module
that prints emoji/Unicode and can run under capture (a scheduled `claude -p`
job, a piped `gh`/CI invocation, ...) needs the fix at import time.

Before this module existed, the identical three-line guard was copy-pasted
verbatim into ten call sites: `browser_verify.py`, `cert_drift.py`,
`chief_ops.py`, `deploy_coverage.py`, `docs_shots_plan.py`,
`e2e_test_audit.py`, `ux_surface.py`, `vendored_drift.py`,
`worktree_claim.py`, and `.claude/skills/sota-watch/watchlist.py` — past this
repo's own "add a helper on the third caller" line (see `no_window.py`,
`git_run.py` for the same reasoning applied earlier). One shared call now
backs all ten.

stdlib only (matches the `_lib` module contract).
"""

from __future__ import annotations

import sys


def ensure_utf8_stdio() -> None:
    """Reconfigure `sys.stdout`/`sys.stderr` to UTF-8, if the stream supports it.

    `hasattr(sys.stdout, "reconfigure")` guards streams that don't have the
    method at all (some test harnesses swap in a plain `io.StringIO`).
    """
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
