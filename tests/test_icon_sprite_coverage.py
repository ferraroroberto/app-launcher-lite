"""Coverage check: every icon() call / #i-NAME reference / {icon: 'NAME'}
property must resolve to a <symbol> defined in the sprite the browser actually
resolves against (#565, #15).

45 icon() calls silently rendered nothing because the vendored sprite never
got the matching symbols, and no test caught the drift. This is the
regression net the issue asked for.

Two things this test must get right, both of which it originally got wrong:

1. The sprite scanned is the *inline* one in index.html, not the vendored
   template. `<use href="#i-NAME">` resolves in-document only (deliberately —
   iOS Safari will not follow an external `<use href="file.svg#id">`), so
   index.html's per-app trim is the only sprite the page ever sees. Checking
   the 86-symbol upstream template instead is a guaranteed false green.
2. The reference scan must see the `{icon: 'NAME'}` property form, not just
   literal `icon('NAME')` calls — `toast()` funnels `opts.icon` straight into
   `icon()` (api.js), and the Jobs/Board status tables carry glyph names the
   same way. That blind spot is how `monitor` and `paperclip` slipped through.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_STATIC_DIR = _REPO_ROOT / "app" / "webapp" / "static"
# The served source of truth: the inline sprite <use> resolves against.
_SPRITE_PATH = _STATIC_DIR / "index.html"

_ICON_REFERENCE_RE = re.compile(
    r"icon\(\s*['\"]([\w-]+)['\"]"  # icon('NAME')
    r"|#i-([\w-]+)"  # <use href="#i-NAME">
    r"|\bicon\s*:\s*['\"]([\w-]+)['\"]"  # { icon: 'NAME' }
)
_SYMBOL_ID_RE = re.compile(r'<symbol\s+id="i-([\w-]+)"')

# icons.js's doc comment uses "#i-NAME" / "icon('NAME')" as literal
# placeholder examples, not real references — the component's own file is
# excluded from the scan.
_EXCLUDED_FILES = {
    _STATIC_DIR / "_vendored" / "icons" / "icons.js",
}


def _is_scanned(path: Path) -> bool:
    """Only files the browser actually loads count as reference sites.

    Every `_vendored/**/*.html` is a component *demo template* — index.html
    links each component's .css/.js, never its .html — so glyphs referenced
    only there (`i-house`, `i-shield-check`, and the sprite template's own
    86 symbols) must not force symbols into the served sprite.
    """
    if path in _EXCLUDED_FILES:
        return False
    parts = path.relative_to(_STATIC_DIR).parts
    return not (parts[0] == "_vendored" and path.suffix == ".html")


def _referenced_icon_names() -> set[str]:
    names: set[str] = set()
    for path in list(_STATIC_DIR.rglob("*.js")) + list(_STATIC_DIR.rglob("*.html")):
        if not _is_scanned(path):
            continue
        text = path.read_text(encoding="utf-8")
        for call_name, href_name, prop_name in _ICON_REFERENCE_RE.findall(text):
            names.add(call_name or href_name or prop_name)
    return names


def _defined_symbol_ids() -> set[str]:
    text = _SPRITE_PATH.read_text(encoding="utf-8")
    return set(_SYMBOL_ID_RE.findall(text))


def test_every_icon_reference_resolves_to_a_sprite_symbol():
    referenced = _referenced_icon_names()
    defined = _defined_symbol_ids()
    missing = sorted(referenced - defined)
    assert not missing, (
        f"{len(missing)} icon()/#i-NAME reference(s) have no matching "
        f"<symbol> in {_SPRITE_PATH.relative_to(_REPO_ROOT)}: {', '.join(missing)}"
    )
