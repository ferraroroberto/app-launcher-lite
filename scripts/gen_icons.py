"""Icon regeneration stub — the lite fork ships the inherited icons.

Icon regeneration requires ``project-scaffolding``'s ``brand_gen`` module,
which is not available alongside this fork. The PWA / tray / favicon assets
(``app/webapp/static/icon-*.png``, ``favicon.ico``,
``assets/tray/app-launcher.ico``) are committed as-is from upstream — edit
them by hand, or restore the upstream ``scripts/gen_icons.py`` from
https://github.com/ferraroroberto/app-launcher if you have a
``project-scaffolding`` checkout to point it at.
"""

from __future__ import annotations

import sys


def main() -> None:
    sys.exit(
        "icon regeneration requires project-scaffolding; the lite fork ships "
        "the inherited icons — edit them by hand or restore the upstream "
        "scripts/gen_icons.py"
    )


if __name__ == "__main__":
    main()
