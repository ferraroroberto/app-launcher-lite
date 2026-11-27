"""Visual proof for a UI change: capture before and after, build `.proof/<MR>/index.html`, declare its state.

The Board's Done lane reads `<projects_dir>/<repo>/.proof/index.json`, keyed by
merge request number, plus `<MR>/index.html` (`src/board/proof_reports.py`).
This package is the writer of that store.

  capture-before|capture-after <repo> [--manifest <path>] [--image "<state>:<viewport>-<theme>=<png>"]...
      Stage one phase's screenshots (before the change, after it) in
      `<this checkout>/.proof/_branch/<branch>/`. A layout-lint manifest also
      carries its findings. The after capture records the tree it was taken on.

  build <repo> --mr <N> [--criterion "<state>=<what changed>"]... [--no-visual "<criterion>"]...
        [--title ...] [--issue N]
      Write `.proof/<N>/index.html` and its index row, then set one
      `VISUAL_PROOF=<state>` line in the merge request description:
        fresh    report built and the tree is still the one the after capture saw
        stale    the tree changed since that capture
        failing  the captures carry layout-lint errors
        missing  no after capture, or the page could not be written
        unknown  the tree or the `.proof/` folder could not be checked
  build <repo> --mr <N> --no-ui
      A change nobody can see: declare `VISUAL_PROOF=n/a`, write no report.
  build <repo> --mr <N> --sha <merge sha>
      After the merge: stamp the sha and copy the report into the primary
      checkout's `.proof/`, which outlives a worktree. The description stays.

Nothing here blocks a merge. A report holds real screenshots, so it stays on
this PC: `.proof/` is never tracked and nothing uploads it to a forge.

Run as a directory: `python <...>/skills/_lib/ui_proof build . --mr 12`.
"""
