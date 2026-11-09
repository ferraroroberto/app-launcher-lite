"""Whether the branch's diff touched anything the session-host loads.

The gate calls this next to the vendored `scripts/classify_e2e.py`. The two answer
different questions: the e2e tier follows the browser surface, while "not live after
a tray restart" follows CLAUDE.md's `## session-host` block (the list `/api/version`'s
`stale_relevant` also reads). A change to e.g. `src/vt_snapshot.py` routes to no
browser tier yet must still warn, so the answer cannot be derived from route labels.

Run with no arguments it reads the live diff; with file arguments it reads that list.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Run standalone, sys.path[0] is scripts/, not the repo root, so `src` and `scripts`
# are unimportable without this.
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.classify_e2e import changed_files  # noqa: E402
from src.session_host_paths import declared_session_host_paths, touched_by  # noqa: E402


def session_host_touched(paths: Sequence[str], claude_md: Path | None = None) -> str:
    """``yes`` / ``no`` / ``unknown``: did ``paths`` touch a declared session-host path.

    ``unknown`` when CLAUDE.md declares none: that is "cannot scope", never "no".
    """
    declared = declared_session_host_paths(claude_md or PROJECT_ROOT / "CLAUDE.md")
    if not declared:
        return "unknown"
    return "yes" if touched_by([p.strip() for p in paths], declared) else "no"


def main(argv: list[str]) -> int:
    paths = argv[1:] if len(argv) > 1 else changed_files()
    print(f"E2E_SESSION_HOST_TOUCHED={session_host_touched(paths)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
