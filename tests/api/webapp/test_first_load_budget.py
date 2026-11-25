"""What a cold first load downloads stays small.

A phone on a slow link pays for every byte before the page can paint and run:
no first-load response may pass 100 KB on the wire, and the render-blocking
pair (the page and its stylesheet) and the whole first load each stay under a
budget with a little headroom over what they weigh now. The page and the
stylesheet are sent without their comments; a change that sends them again
fails here. The views a tap opens (chat, terminal, changes, links, Screen,
Progress, the Jobs, Team OS and Board tabs, the Settings pane, the Markdown
renderer, the Apps dialogs and the shared composer) load
on that tap (lazy-views.js), and what only runs once boot has settled (the
new-build prompt, the build readout and restart sheet, the copy button of
rendered Markdown, the terminal scripts' loader) loads then; a static import
that pulls one back into the first load fails here too. The first load
measured here is a Code-first boot: main.js's graph plus the Code and Apps
tabs' code it evaluates before its wiring. A boot that reopens on the Board
preloads only what its lanes draw: the Code and Apps tabs' code, the card
drawer, the answer sheets, the plan note sheet and the journal card load after.
"""

from __future__ import annotations

import posixpath
import re
from typing import Dict, List

MAX_RESPONSE_BYTES = 100 * 1024
# The page and its one stylesheet block the first paint (55 KB together, gzipped).
RENDER_BLOCKING_BYTES = 70 * 1024
# Page, stylesheet, main.js's static graph and the Code tab's code (136 KB now).
FIRST_LOAD_BYTES = 140 * 1024
# Loaded on first open, never by the first load.
LAZY_VIEWS = (
    "/static/terminal.js",
    "/static/session-overlay.js",
    "/static/chat/session-transcript.js",
    "/static/changes/changes-overlay.js",
    "/static/links/links-sheet.js",
    "/static/screen/screen-view.js",
    "/static/progress/progress-sheet.js",
    "/static/health/machine-health.js",
    "/static/jobs.js",
    "/static/board.js",
    "/static/board/board-answers.js",
    "/static/board/board-dispatch.js",
    "/static/board/drawer.js",
    "/static/composer.js",
    "/static/voice.js",
    "/static/team-os.js",
    "/static/team-os/team-os-content.js",
    "/static/settings/settings-sheets.js",
    "/static/settings/performance.js",
    "/static/settings/documents.js",
    "/static/settings/tokens.js",
    "/static/settings/diagnostics.js",
    "/static/settings/lid-keep-awake.js",
    "/static/settings/install-readiness.js",
    "/static/_vendored/fullscreen/fullscreen.js",
    "/static/_vendored/text-size/text-size.js",
    "/static/markdown.js",
    "/static/shared/mermaid-render.js",
    "/static/settings/speech-settings.js",
    "/static/settings/network-access.js",
    "/static/settings/mirror-browser.js",
    "/static/settings/launch-presets-settings.js",
    "/static/settings/serving.js",
    "/static/apps/apps-dialogs.js",
    "/static/quote-copy.js",
    "/static/_vendored/build-watch/build-watch.js",
    "/static/_vendored/page-foot/page-foot.js",
    "/static/shared/build-restart.js",
    "/static/terminal/xterm-loader.js",
    "/static/chat/transcript-text.js",
)
# Never preloaded by a Board-first boot: none of it draws the lanes.
BOARD_LATER = (
    "/static/apps.js",
    "/static/apps/apps-coding.js",
    "/static/apps/apps-listeners.js",
    "/static/apps/git-sync.js",
    "/static/launch-toolbar.js",
    "/static/list-filter.js",
    "/static/shared/launch-presets.js",
    "/static/lightbox.js",
    "/static/_vendored/sign-in/sign-in.js",
    "/static/board/drawer.js",
    "/static/markdown.js",
    "/static/board/board-answers.js",
    "/static/board/owner-answer.js",
    "/static/board/answer-files.js",
    "/static/attach-batch.js",
    "/static/board/plan-note-sheet.js",
    "/static/board/chief-journal.js",
)

_STATIC_IMPORT_RE = re.compile(r"""from\s*['"](\.\.?/[^'"?]+\.js)(\?v=[^'"]*)?['"]""")
_ENTRY_RE = re.compile(r'<script type="module" src="(/static/main\.js[^"]*)"')
# The Code tab's code: the first link of the page's apps preload.
_APPS_ENTRY_RE = re.compile(r'<template id="appsPreload">\s*<link rel="modulepreload" href="([^"]+)">')
_STYLESHEET_RE = re.compile(r'<link rel="stylesheet" href="(/static/[^"]+)"')


def _wire_bytes(client, url: str) -> int:
    resp = client.get(url, headers={"Accept-Encoding": "gzip"})
    assert resp.status_code == 200, url
    return resp.num_bytes_downloaded


def _first_load(client) -> Dict[str, int]:
    """Wire bytes of the page, its stylesheets and main.js's static import graph."""
    page = client.get("/", headers={"Accept-Encoding": "gzip"})
    sizes = {"/": page.num_bytes_downloaded}
    for url in _STYLESHEET_RE.findall(page.text):
        sizes[url] = _wire_bytes(client, url)
    queue: List[str] = _ENTRY_RE.findall(page.text) + _APPS_ENTRY_RE.findall(page.text)
    assert len(queue) == 2, "the page lost its module entry or its Code tab preload"
    while queue:
        url = queue.pop()
        if url in sizes:
            continue
        resp = client.get(url, headers={"Accept-Encoding": "gzip"})
        assert resp.status_code == 200, url
        sizes[url] = resp.num_bytes_downloaded
        base = posixpath.dirname(url.split("?")[0])
        for spec, stamp in _STATIC_IMPORT_RE.findall(resp.text):
            queue.append(posixpath.normpath(posixpath.join(base, spec)) + (stamp or ""))
    return sizes


def test_no_first_load_response_passes_100_kb(webapp_client):
    client, _, _ = webapp_client
    sizes = _first_load(client)
    assert len(sizes) > 40, "the module graph walk found too little to be the real page"
    over = {url: size for url, size in sizes.items() if size > MAX_RESPONSE_BYTES}
    assert not over, over


def test_the_first_paint_and_the_whole_first_load_stay_in_budget(webapp_client):
    client, _, _ = webapp_client
    sizes = _first_load(client)
    blocking = sizes["/"] + sum(size for url, size in sizes.items() if url.endswith(".css") or ".css?" in url)
    assert blocking <= RENDER_BLOCKING_BYTES, f"page + stylesheet: {blocking} bytes"
    total = sum(sizes.values())
    assert total <= FIRST_LOAD_BYTES, f"first load: {total} bytes over {len(sizes)} responses"


def test_the_views_a_tap_opens_stay_out_of_the_first_load(webapp_client):
    client, _, _ = webapp_client
    loaded = {url.split("?")[0] for url in _first_load(client)}
    assert "/static/lazy-views.js" in loaded, "the walk never reached the lazy loader"
    assert not loaded.intersection(LAZY_VIEWS), sorted(loaded.intersection(LAZY_VIEWS))


_TEMPLATE_RE = r'<template id="{}">(.*?)</template>'
_PRELOAD_RE = re.compile(r'<link rel="modulepreload" href="([^"]+)">')
_BOARD_IMPORT_RE = re.compile(r"""import\(\s*['"]\./board\.js(\?v=[^'"]*)?['"]""")
_APPS_IMPORT_RE = re.compile(r"""import\(\s*['"]\./apps\.js(\?v=[^'"]*)?['"]""")


def _preloads(page: str, template_id: str) -> List[str]:
    match = re.search(_TEMPLATE_RE.format(template_id), page, re.S)
    assert match, f"no <template id={template_id!r}> in the page"
    return _PRELOAD_RE.findall(match.group(1))


def test_the_page_preloads_exactly_the_first_load_modules(webapp_client):
    """The whole static graph is asked for at once; a preload URL that differs
    from the import's (another ?v=) would fetch a second, unused copy."""
    client, _, _ = webapp_client
    page = client.get("/").text
    preloads = _preloads(page, "modulePreload")
    apps = _preloads(page, "appsPreload")
    modules = [url for url in _first_load(client) if ".js" in url]
    assert len(preloads) == len(set(preloads))
    assert not set(preloads).intersection(apps)
    assert sorted(preloads + apps) == sorted(modules)


def test_a_board_first_boot_preloads_the_instance_lazy_views_imports(webapp_client):
    """The Board's own modules, none of the first load's, starting with the
    board.js URL lazy-views.js imports, or the Board would load twice as two
    module instances."""
    client, _, _ = webapp_client
    page = client.get("/").text
    board = _preloads(page, "boardPreload")
    first = set(_preloads(page, "modulePreload"))
    assert board and not first.intersection(board)
    stamps = _BOARD_IMPORT_RE.findall(client.get("/static/lazy-views.js").text)
    assert stamps, "lazy-views.js no longer imports the Board"
    assert board[0] == "/static/board.js" + stamps[0]
    assert "/static/board/chief-plan.js" + stamps[0] in board


def test_a_board_first_boot_preloads_only_what_the_lanes_draw(webapp_client):
    client, _, _ = webapp_client
    page = client.get("/").text
    preloaded = _preloads(page, "modulePreload") + _preloads(page, "boardPreload")
    board = {url.split("?")[0] for url in preloaded}
    assert not board.intersection(BOARD_LATER), sorted(board.intersection(BOARD_LATER))


def test_any_other_boot_preloads_the_code_tab_instance_lazy_views_imports(webapp_client):
    """main.js and lazy-views.js import apps.js by the same URL the page
    preloads, or the Code tab would load twice as two module instances."""
    client, _, _ = webapp_client
    page = client.get("/").text
    apps = _preloads(page, "appsPreload")
    for module in ("/static/main.js", "/static/lazy-views.js"):
        stamps = _APPS_IMPORT_RE.findall(client.get(module).text)
        assert stamps, f"{module} no longer imports the Code tab's code"
        assert apps[0] == "/static/apps.js" + stamps[0]
