"""The transcript renderer is its own module, free of the Chat pane (#1629).

`renderEntries` used to live in `session-transcript.js`, the Chat pane's
controller, and read that module's `view` singleton for the open session's id
and project folder, so the renderer depended on the Chat pane's state. The renderer now lives in `transcript-render.js` and is
handed the session as a parameter.

These tests pin that as a property: the renderer never imports the controller
or names its `view`,
and, like the markdown renderer it shares, it belongs to no single e2e
surface, so a change to it runs the full browser suite.
"""

from __future__ import annotations

import pathlib
import re

from scripts.classify_e2e import classify, load_config

STATIC = pathlib.Path(__file__).resolve().parents[3] / "app" / "webapp" / "static"
RENDER_MODULE = "app/webapp/static/chat/transcript-render.js"

_IMPORT_RE = re.compile(r"^import\s+(?:\{[^}]*\}|[\w*\s,]+)\s+from\s+'([^']+)';", re.M)


def _read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def _imports(name: str) -> set[str]:
    return set(_IMPORT_RE.findall(_read(name)))


def test_the_renderer_never_reads_the_chat_panes_state():
    assert "./session-transcript.js" not in _imports("chat/transcript-render.js")
    source = _read("chat/transcript-render.js")
    assert not re.search(r"(?<![\w-])view\.", source), (
        "the renderer is handed the session (opts.session); it never reads the Chat pane's view"
    )


def test_a_renderer_change_runs_the_full_suite():
    cfg = load_config()
    for surface in cfg.surfaces:
        assert RENDER_MODULE not in surface.paths, (
            f"transcript-render.js is claimed by the {surface.name!r} surface, which would "
            "narrow verification to that surface alone"
        )
    result = classify([RENDER_MODULE], cfg)
    assert result.tier == "full", f"a renderer-only change must run the full suite, got {result.tier!r}"
