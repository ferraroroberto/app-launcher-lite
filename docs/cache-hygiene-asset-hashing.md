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
  stylesheet under 70 KB, and the whole first load (page, stylesheet and every
  module `main.js` reaches statically) under 220 KB.
- The views a tap opens load on that tap, not with the page
  (`app/webapp/static/lazy-views.js`): Chat, the terminal, the changes overlay,
  Links, Screen, Progress, the Jobs tab, the Board's answer sheet and the
  shared composer were about half of the first load's JavaScript, and none of
  them draws the shell, the session list or the Board. Every caller goes
  through `lazy-views.js`, which loads and wires each group once. Once the
  session list has drawn, the session views load straight away and the rest
  when the phone goes idle, so a later first open finds them parsed. A tap on
  the Jobs pane that beats its code is held and replayed, and a deep link
  (`?terminal=`, `?session=`, `?screen=1`) starts its view's load at boot. The
  predicates the session list draws from (which modes a session offers, the
  one it opens in) live in `session-modes.js`, apart from the views.
  `tests/api/webapp/test_first_load_budget.py` fails when a static import pulls
  one of these views back into the first load.
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
- Build-time bundler (Vite / esbuild) — vanilla JS by design.
- CI hooks for a validation pipeline on this mechanism.
