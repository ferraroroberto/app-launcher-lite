"""Make the package directory executable: `python <...>/skills/_lib/ui_proof build . --mr 12`.

Directory execution puts this directory on `sys.path[0]`, not `skills/_lib`,
so the parent is added before importing the package by name.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ui_proof.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
