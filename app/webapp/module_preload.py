"""Expand ``index.html``'s module-preload markers into ``<link rel="modulepreload">`` tags.

A browser finds an ES module's imports only once the module has arrived, so a
graph six imports deep costs six round trips before its last file is even
requested. On a phone's 150 ms link that depth, not the bytes, was most of a
cold load. A marker line in ``index.html``::

    <!-- @modulepreload main.js -->
    <!-- @modulepreload board.js -main.js -->

becomes one ``modulepreload`` link per module in the entry's static import
closure (``import ... from './x.js'``, never a dynamic ``import()``), minus the
closure of every ``-``-prefixed module, so the page asks for the whole graph at
once. The links are unstamped here; ``rewrite_index_html`` stamps them with the
same ``?v=`` the imports carry, so each preload is the instance an import uses.

When the page loads the ES-module bundle the same markers name the bundle's
chunks instead: ``closure`` maps an entry to the chunks it needs, read from
the bundle's chunk graph.
"""

from __future__ import annotations

import posixpath
import re
from functools import partial
from pathlib import Path
from typing import Callable, List, Optional, Set

from app.webapp.static_trim import trim_js

# The whole line is the marker, like shell_html's include marker.
_MARKER_RE = re.compile(r"^([ \t]*)<!-- @modulepreload ([^>]+?) -->[ \t]*$", re.MULTILINE)
_STATIC_IMPORT_RE = re.compile(r"""\bfrom\s*['"](\.\.?/[^'"?]+\.js)(?:\?v=[^'"]*)?['"]""")


def static_import_closure(static_dir: Path, entry: str) -> List[str]:
    """``entry`` and every module it reaches through static imports, static-dir
    relative, in the order a browser discovers them (breadth first).

    A module that is missing on disk is left out rather than failing the page:
    the browser's own import then reports it.
    """
    seen: Set[str] = set()
    order: List[str] = []
    queue = [posixpath.normpath(entry)]
    while queue:
        rel = queue.pop(0)
        if rel in seen:
            continue
        seen.add(rel)
        try:
            body = (static_dir / rel).read_text(encoding="utf-8")
        except OSError:
            continue
        order.append(rel)
        base = posixpath.dirname(rel)
        for spec in _STATIC_IMPORT_RE.findall(trim_js(body)):
            queue.append(posixpath.normpath(posixpath.join(base, spec)))
    return order


def expand_preloads(shell: str, static_dir: Path,
                    closure: Optional[Callable[[str], List[str]]] = None) -> str:
    """``shell`` with every module-preload marker replaced by its links.

    ``closure`` maps an entry to the static-dir-relative files it loads; the
    default follows the source modules' static imports.
    """
    resolve = closure or partial(static_import_closure, static_dir)

    def _sub(match: re.Match) -> str:
        indent, spec = match.group(1), match.group(2).split()
        include = [name for name in spec if not name.startswith("-")]
        exclude: Set[str] = set()
        for name in spec:
            if name.startswith("-"):
                exclude.update(resolve(name[1:]))
        modules: List[str] = []
        for name in include:
            modules += [m for m in resolve(name)
                        if m not in exclude and m not in modules]
        return "\n".join(f'{indent}<link rel="modulepreload" href="/static/{m}">' for m in modules)

    return _MARKER_RE.sub(_sub, shell)
