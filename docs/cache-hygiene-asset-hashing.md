# Cache hygiene: content-hash asset stamping + /api/version

Every `.js` / `.css` under `app/webapp/static/` (excluding `vendor/`) is hashed
at app startup. A single **fleet hash** (sha256 over the sorted per-file
hashes, first 8 hex chars) is stamped onto every asset URL — both in
`index.html` and on the ES-module `import './foo.js'` lines and dynamic
`import('./foo.js')` calls inside JS files (rewritten at serve time, not on
disk). A dynamic import carries the same stamp as the static ones because an
unstamped URL would be a second copy of the module with its own state. This is how the webapp guarantees the
iPhone PWA never serves a stale asset against fresh HTML, or vice versa.

- The static mount is a `_VersionedStatic(StaticFiles)` subclass that sets
  `Cache-Control: public, max-age=31536000, immutable` on hashed assets,
  `public, max-age=86400` on icons / manifest, defaults elsewhere. Safe to be
  aggressive because the URL changes on edit.
- `index.html` keeps `no-cache, must-revalidate` so the hashed asset URLs the
  phone resolves are always fresh.
- Text responses over 1,000 bytes (HTML, JS, CSS, JSON, SVG) are gzipped when
  the client sends `Accept-Encoding: gzip`, by the fleet's vendored
  `StreamSafeGZipMiddleware` (`src/compression.py`, registered last so it is
  the outermost layer). It decides from the response headers: images other
  than SVG, audio, video, PDFs and fonts are already compressed, and the TTS
  proxy's `audio/L16` and every SSE stream must reach the phone chunk by
  chunk, which gzip's buffering would stall. Websockets never pass through
  it.
- Served `.js` modules (not `vendor/`) go out without their comments
  (`app/webapp/static_trim.py`): the explanatory comments were about 45% of the
  gzipped module graph a cold launch fetches. The files on disk are untouched,
  the fleet hash is computed from them, and the trimmed copy is kept in memory
  until the file changes. Strings, template literals, regex literals and
  `/*! */` banners are copied verbatim, and a source the scanner cannot follow
  to its end is sent unchanged. `tests/unit/static_ui/test_static_trim.py` runs
  every shipped module through `node --check`.
- The page and its stylesheet bundle go out without their comments too,
  because both block the first paint: `trim_shell` (`app/webapp/shell_html.py`)
  drops HTML comments outside `script`, `style`, `pre` and `textarea`, and
  `trim_css` (`src/stylesheet_bundle.py`) drops CSS comments outside strings,
  keeping `/*! */` banners. Comments are about two thirds of the bundle's
  gzipped bytes and a quarter of the page's. `tests/api/webapp/test_first_load_budget.py`
  holds every first-load response under 100 KB on the wire, the page plus its
  stylesheet under 70 KB, and the whole first load of a Code-first boot (page,
  stylesheet, every module `main.js` reaches statically and the Code tab's
  code) under 140 KB.
- The views a tap opens load on that tap, not with the page
  (`app/webapp/static/lazy-views.js`): Chat, the terminal, the changes overlay,
  Links, Screen, Progress, the Jobs, Team OS and Board tabs, the Settings
  pane and the rows Settings alone draws (speech, private network, mirror
  browser, launch presets), the Apps tab's rename and scan dialogs, the
  Markdown renderer (`lightbox.js` imports it with the first Markdown file it
  draws, the Board drawer with its first exchange) and the shared composer
  were about two thirds of the first load's JavaScript, and none of them
  draws the shell or the session list. What runs only once the list has drawn
  or boot has settled loads then, from `main.js`: the new-build prompt, the
  build readout and its restart sheet, the copy button of rendered Markdown,
  the time formatter and the terminal scripts' loader. The sign-in sheet loads
  on the first 401, and the image and HTML viewer (`lightbox.js`) with the
  first view that opens one. Every caller of a view
  goes through `lazy-views.js`,
  which loads and wires each group once. Once the session list has drawn, the
  session views load straight away and the rest when the phone goes idle, so a
  later first open finds them parsed. A tap on the Jobs, Team OS, Board, Apps or
  Settings pane, or on the Code tab's projects card, that beats its code is
  held and replayed, a Board card opened
  before the drawer's code shows a loading drawer, and a deep link (`?terminal=`,
  `?session=`, `?screen=1`) starts its view's load at boot. The predicates the
  session list draws from (which modes a session offers, the one it opens in)
  live in `session-modes.js`, and the chief row's Start, Resume and Restart in
  `board/chief-actions.js`, apart from the views.
  `tests/api/webapp/test_first_load_budget.py` fails when a static import pulls
  one of these views back into the first load.
- The page asks for the whole first-load module graph at once. A browser finds
  a module's imports only when the module arrives, so the graph's import depth
  cost one round trip per level on a phone link. `app/webapp/module_preload.py`
  expands `index.html`'s `<!-- @modulepreload main.js -->` marker into one
  `<link rel="modulepreload">` per module `main.js` reaches statically, and
  `rewrite_index_html` stamps them with the imports' `?v=`. The links sit inert
  in a `<template>`; an inline script after the stylesheets adds them, so they
  never compete with the first paint. It adds them once per `?v=` stamp: a build
  the browser has loaded before is in its cache, where preloading only costs
  the warm load parse time. A boot that reopens on the Board, or lands on a
  `?board=` link, also adds the Board's own modules (the `board.js` marker
  minus `main.js`'s), and `main.js` evaluates the Board before its wiring and
  hands the module to `lazy-views.js`. Those are only what the lanes draw: the
  card drawer, the answer sheets (`board/owner-answer.js` draws an owner check's
  block there), the plan note sheet and the journal card load after the lanes,
  through `lazy-views.js`. Every other boot but the Screen pop-out adds the Code
  and Apps tabs' code instead (the `apps.js` marker minus `main.js`'s), which
  `main.js` evaluates the same way; a Board-first boot loads it after the lanes
  draw, or on the first visit to either tab. The registry and agent reads the
  Board keys on run without it (`apps/apps-reads.js`), and the launch toolbars'
  preset pickers redraw on a `launcher:config` event, so no first-load module
  imports that code. Over HTTP/1.1 each module costs a slot on one of six
  connections, so the module count, more than the bytes, sets when the lanes
  draw; over HTTP/2 the modules share one connection and arrive at about the
  line rate, so the bytes do. The preloads follow the import graph as
  it was at the first page render after a restart.
- The served page loads the modules as one bundle. Even preloaded, each module
  is its own response, and on the phone profile responses finish about 15 ms
  apart whatever their size, so about 60 modules, not their bytes, set when the
  Board's lanes draw. After start, in a background thread, the server bundles `main.js`'s graph with a
  pinned esbuild (`src/static_bundles/js_bundle.py`, run through `npx`) into an
  entry, shared chunks and one chunk per module some code loads with
  `import()`, so every lazy view above still loads on first open. The chunks
  are minified for whitespace and syntax but keep their names, and source maps
  are served beside them. The build is cached under `webapp/js-bundle/` per
  fleet hash: any module edit rebuilds it on the next start, and a restart with
  no edit reads the cache. The build never holds up startup: a cold run can
  take minutes while `npx` fetches esbuild, so `/healthz` answers at once and
  a page loaded before the build lands gets the separate modules; the bundle
  routes answer 404 until then and serve the bundle after, and the next page
  load switches over. Every bundle file takes the fleet hash, and each
  chunk import carries it, because a chunk loaded under two URLs would be two
  module instances. esbuild's metafile records which chunk each source module
  became, so the same `@modulepreload` markers name the chunks a boot needs: a
  Board-first boot asks for about 21 responses before its lanes instead of
  about 60. Without Node, or when the build fails, the server logs a warning
  and serves `main.js` and its modules separately, with the module preloads
  above. A request carrying the cookie `launcher_js_bundle=off` gets the
  separate modules too. On the phone profile (4x CPU, 150 ms round trip,
  1.6 Mbps, median of 15 alternated cold loads) the bundle took the Board from
  about 1.65 s to 1.38 s over HTTP/2 and from 2.63 s to 1.57 s over HTTP/1.1,
  and the Code-first session list from 1.41 s to 1.40 s over HTTP/2.
- The page starts its first reads before the module graph has loaded. An
  inline script in `index.html` fetches `/api/config`, `/api/agents`,
  `/api/apps` and `/api/claude-code/sessions`, plus the restored tab's own read
  (`/api/board` and `/api/board/chief-plan`, or `/api/jobs`). `api.js` takes each
  response once, for a plain GET within 15 s; any later call fetches afresh.
  The restored Board or Jobs tab reads at boot instead of on its first poll,
  and the Board's lanes never wait on the chief-plan read.
  `tests/e2e/shell/test_first_load_sources.py` holds every other source
  unanswered with the page clock paused and checks the Board and the session
  list still draw.
- `GET /api/version` returns `{git_sha, built_at, asset_hash, head_sha,
  session_host, age_seconds}`. `git_sha`/`built_at` are cached at module
  import; `head_sha` and `session_host` come from a snapshot the webapp
  rebuilds every 5 s while the route is polled (`age_seconds` is its age;
  one older than 15 s is rebuilt before it answers). Pythonw-safe subprocess
  (`stdin=DEVNULL`, `CREATE_NO_WINDOW`, `git -C <root>`).
  The SPA fetches it via `jsonApi`
  (auth-gated like every other API) and renders `Build: <sha> · <ts>` at the
  bottom of the Settings panel — visible proof of which build the phone is
  running.

## Why fleet-hash, not per-file hash

The ES-module graph contains a cycle (`sessions.js ↔ terminal.js`), so
per-file transitive hashing would need SCC handling. With ~10 small files
totalling ~150 KB, the cost of invalidating all asset URLs on any edit is
well under a second on LTE — not worth the complexity.

## How it works in practice

The day-to-day loop, for anyone editing static files later:

1. Edit any `.js` / `.css` under `app/webapp/static/` (not `vendor/`).
2. Restart the `:8465` webapp. The fleet hash is computed **once at
   startup** — not per request — so an unrestarted process keeps serving the
   old hash even though the files on disk changed.
3. The phone re-fetches `index.html` (it is served `no-cache,
   must-revalidate`, so it is never stale). The server stamps the new
   `?v=<hash>` onto every asset URL **at request time** — the query string
   is not written to the file on disk.
4. Every asset URL the phone resolves is one it has never cached, so it
   downloads the new bytes. Unchanged files keep their previous URL and stay
   served from the phone's cache.
5. Confirm the build that is actually live with `GET /api/version` —
   `asset_hash` should have changed; `git_sha` should match `HEAD`.

The hash *is* the content: it changes if and only if the bytes change. The
only manual step left is the restart in step 2.

## A page left open across a deploy

The hash also lets a page that never reloaded notice the move. The vendored `_vendored/build-watch/build-watch.js` reads the `?v=` stamp the page booted on and compares it with `asset_hash` from `GET /api/version` when the page becomes visible (at most once per 60 s) and every 5 minutes while visible. A difference raises a "New version available" prompt with a **Reload** button and a dismiss; it never reloads by itself, so an unsent composer draft survives. A dismissed prompt stays down until the server moves to yet another build. Unknown is not stale: a tree with no hashes (no `?v=`), an empty `asset_hash`, a failed read or a 401 show nothing. A failed read is not counted against the throttle, so the next trigger retries it.

## Why not a manual `?v=N` query string

A hand-bumped version number (`styles.css?v=18 → ?v=19` in `index.html`)
carries three distinct failure modes that content-hash stamping removes:

| Manual `?v=N` | Content-hash stamping |
|---|---|
| **You must remember to bump it.** Forget, and the phone serves stale CSS/JS against new HTML — the exact bug `test_primary_nav.py`'s WebKit `Cache-Control` check (formerly `test_iphone_revalidate.py`) pins. | Impossible to forget — the hash is derived from the file bytes. |
| **Only a subset of files would carry a `?v=`** unless every ES-module import line is hand-maintained too, which nothing enforces. | Covers every `.js`/`.css`, including the `import` lines between modules. |
| **`N` is arbitrary.** It records "I bumped it," not "the content changed" — a bump with no change still busts cache; a change with no bump does not. | The hash tracks content exactly: no false busts, no missed ones. |

Consequence for later work: **do not reintroduce `?v=N` query strings in
`index.html`.** They are redundant with the fleet hash and reintroduce the
manual step this mechanism exists to kill.

## Out of scope (deliberately)

- Service-worker offline cache — separate, much larger change.
- A build-time bundler. The server bundles the ES modules itself in the background after start (`src/static_bundles/js_bundle.py`), so contributors still run no build step.
- CI hooks for a validation pipeline on this mechanism.
