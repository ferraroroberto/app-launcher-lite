"""What a cold first load downloads stays small.

A phone on a slow link pays for every byte before the page can paint and run:
no first-load response may pass 100 KB on the wire, and the render-blocking
pair (the page and its stylesheet) and the whole first load each stay under a
budget with a little headroom over what they weigh now. The page and the
stylesheet are sent without their comments; a change that sends them again
fails here. The views a tap opens (chat, terminal, changes, links, Screen,
Progress, the Jobs tab, the Board's answer sheet and the shared composer) load
on that tap (lazy-views.js); a static import that pulls one back into the
first load fails here too.
"""

from __future__ import annotations

import posixpath
import re
from typing import Dict, List

MAX_RESPONSE_BYTES = 100 * 1024
# The page and its one stylesheet block the first paint (55 KB together, gzipped).
RENDER_BLOCKING_BYTES = 70 * 1024
# Page, stylesheet and every module main.js reaches statically (202 KB now).
FIRST_LOAD_BYTES = 220 * 1024
# Loaded on first open, never by the first load.
LAZY_VIEWS = (
    "/static/terminal.js",
    "/static/session-overlay.js",
    "/static/chat/session-transcript.js",
    "/static/changes/changes-overlay.js",
    "/static/links/links-sheet.js",
    "/static/screen/screen-view.js",
    "/static/progress/progress-sheet.js",
    "/static/jobs.js",
    "/static/board/board-answers.js",
    "/static/composer.js",
    "/static/voice.js",
)

_STATIC_IMPORT_RE = re.compile(r"""from\s*['"](\.\.?/[^'"?]+\.js)(\?v=[^'"]*)?['"]""")
_ENTRY_RE = re.compile(r'<script type="module" src="(/static/main\.js[^"]*)"')
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
    queue: List[str] = _ENTRY_RE.findall(page.text)
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
    assert len(sizes) > 50, "the module graph walk found too little to be the real page"
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
