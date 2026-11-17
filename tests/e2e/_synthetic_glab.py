"""A stand-in ``glab`` for the disposable e2e webapp: no network, no rows.

``src.clients.gitlab`` runs the argv ``LAUNCHER_GLAB_CMD`` names instead of the
real ``glab``. Any test that opens the Board without mocking ``/api/board``
sets off the GitLab refresh, and a gate run must never reach real GitLab, so
this answers every ``glab api`` read with an empty page: an issue or merge
request list is ``[]``, anything else (``/user``) an empty object. A write
(``--method``) echoes an empty object. Tests that need Board rows mock
``/api/board`` themselves.

Run as ``python _synthetic_glab.py <glab args>``; stdlib only.
"""

from __future__ import annotations

import sys


def main(argv: list) -> int:
    if not argv or argv[0] != "api":
        sys.stderr.write("synthetic glab: only `glab api` is supported\n")
        return 1
    path = argv[1] if len(argv) > 1 else ""
    if "--method" in argv:
        sys.stdout.write("{}")
    elif "issues" in path or "merge_requests" in path:
        sys.stdout.write("[]")
    else:
        sys.stdout.write("{}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
