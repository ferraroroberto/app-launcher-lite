"""Build `.proof/<MR>/index.html`: per criterion, what changed, then before and after side by side."""

from __future__ import annotations

import base64
import html
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from fleet_core import runtime_files

from ui_proof import proof, store

# The largest report the Board's reader serves (`src/board/proof_reports.MAX_REPORT_BYTES`).
MAX_REPORT_BYTES = 40 * 1024 * 1024

# The reader serves the page under a scriptless policy: inline CSS and `data:` images only.
_CSS = """
:root{--bg:#f7f7f8;--card:#fff;--fg:#1d1d1f;--muted:#6e6e73;--line:#d2d2d7;--ok:#1a7f37;--bad:#c62828}
@media (prefers-color-scheme:dark){:root{--bg:#111113;--card:#1c1c1e;--fg:#f5f5f7;--muted:#a1a1a6;
--line:#3a3a3c;--ok:#3fb950;--bad:#ff6b6b}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:1100px;margin:0 auto;padding:16px}
h1{font-size:22px;margin:8px 0}h2{font-size:17px;margin:0 0 4px}h3{font-size:14px;margin:12px 0 4px}
.meta,.small{color:var(--muted);font-size:13px}
section{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;margin:14px 0}
.pair{display:grid;grid-template-columns:1fr 1fr;gap:10px}
figure{margin:0}figure img{width:100%;height:auto;border:1px solid var(--line);border-radius:8px}
figcaption{font-size:13px;color:var(--muted)}.ok{color:var(--ok)}.bad{color:var(--bad)}
code{font-size:13px;word-break:break-all}
"""


def _image(path: Path) -> Optional[str]:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")


def _figure(folder: Path, row: Optional[dict], phase: str) -> str:
    src = _image(folder / str(row.get("file"))) if row else None
    img = f'<img alt="{phase}" src="{src}">' if src else f'<p class="small">no {phase} capture</p>'
    note = ""
    if row and "findings" in row:
        errors = sum(proof.finding_fails(f) for f in row["findings"])
        note = ' — <span class="ok">lint clean</span>' if not errors else f' — <span class="bad">{errors} error(s)</span>'
    return f"<figure>{img}<figcaption>{phase}{note}</figcaption></figure>"


def _legs(rows: List[dict], state: str) -> Dict[str, dict]:
    return {f"{r.get('viewport')}-{r.get('theme')}": r for r in rows if r.get("state") == state}


def _state_section(folder: Path, data: dict, state: str, lines: List[str]) -> str:
    before, after = _legs(data.get("before", []), state), _legs(data.get("after", []), state)
    legs = [k for k in store.LEG_ORDER if k in before or k in after]
    legs += sorted((set(before) | set(after)) - set(legs))
    rows = "".join(
        f"<h3>{html.escape(leg.replace('-', ' · '))}</h3><div class=\"pair\">"
        f"{_figure(folder, before.get(leg), 'before')}{_figure(folder, after.get(leg), 'after')}</div>"
        for leg in legs)
    if len(lines) > 1:
        heading = "<ul>" + "".join(f"<li>{html.escape(t)}</li>" for t in lines) + "</ul>"
    else:
        heading = f"<h2>{html.escape(lines[0]) if lines else 'Other captured state'}</h2>"
    return (f"<section>{heading}<div class=\"small\">state <code>{html.escape(state)}</code></div>"
            f"{rows}</section>")


def _lint_block(data: dict) -> str:
    linted = [r for r in data.get("after", []) if "findings" in r]
    if not linted:
        return '<p class="small">Layout lint: not run (no layout-lint manifest for the after capture).</p>'
    errors = proof.failing_lines(linted)
    if not errors:
        return '<p class="ok">Layout lint: clean on every linted leg.</p>'
    return ('<p class="bad">Layout lint errors:</p><ul>'
            + "".join(f"<li><code>{html.escape(e)}</code></li>" for e in errors) + "</ul>")


def render(repo_name: str, mr: int, data: dict, folder: Path) -> str:
    """The self-contained page (inline CSS, `data:` images, no scripts)."""
    criteria: Dict[str, List[str]] = data.get("criteria") or {}
    no_visual: List[str] = data.get("no_visual") or []
    states = {str(r.get("state")) for p in store.PHASES for r in data.get(p, [])}
    sections = [_state_section(folder, data, s, lines) for s, lines in criteria.items()]
    sections += [_state_section(folder, data, s, []) for s in sorted(states - set(criteria))]
    sections += [f"<section><h2>{html.escape(t)}</h2><p class=\"small\">No visual change.</p></section>"
                 for t in no_visual]
    sha = data.get("merge_sha") or "not merged yet"
    issue = f" · issue #{data['issue']}" if data.get("issue") else ""
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>Proof {html.escape(repo_name)}!{mr}</title><style>{_CSS}</style></head><body><main>"
        f"<h1>{html.escape(data.get('title') or f'{repo_name} MR !{mr}')}</h1>"
        f"<div class=\"meta\">{html.escape(repo_name)} MR !{mr}{issue} · merge <code>{html.escape(sha)}</code>"
        f" · state {html.escape(data.get('state', ''))} · built {html.escape(data.get('utc', ''))}</div>"
        + _lint_block(data) + "".join(sections) + "</main></body></html>"
    )


def _write(repo_name: str, mr: int, folder: Path, data: dict) -> Tuple[Optional[Path], str]:
    """Write the page, its data and its index row; `(page, "")` or `(None, why)`."""
    page_text = render(repo_name, mr, data, folder)
    size = len(page_text.encode("utf-8"))
    if size > MAX_REPORT_BYTES:
        return None, (f"the page is {size} bytes, over the {MAX_REPORT_BYTES} the Board serves; "
                      "capture fewer legs or smaller states")
    store.save_data(folder, data)
    page = folder / store.PAGE_NAME
    runtime_files.save_text(page, page_text, encoding="utf-8")
    reason = store.update_index(folder.parent, mr, {k: data.get(k) for k in
                                                    ("title", "issue", "merge_sha", "utc", "branch")})
    return (None, reason) if reason else (page, "")


def build(repo: Path, repo_name: str, mr: int, criteria: Dict[str, List[str]], no_visual: List[str],
          title: str = "", issue: Optional[int] = None) -> Tuple[str, List[str]]:
    """Write `.proof/<MR>/index.html` before the merge; `(state, KEY=value lines)`.

    Without an after capture no page is written: the Board offers Proof for any
    stored report, so a before-only page would pass as proof of the change.
    """
    refused = store.ensure_ignored(repo)
    if refused:
        return proof.UNKNOWN, [f"PROOF_PAGE=not_built reason={refused}"]
    folder = store.prepare_mr_folder(repo, mr)
    if folder is None:
        return proof.UNKNOWN, ["PROOF_PAGE=not_built reason=not a git checkout"]
    data = store.load_data(folder)
    state, why = proof.judge(repo, data)
    if state == proof.MISSING:
        return state, [f"PROOF_PAGE=not_built reason=no after capture for this branch or MR {mr} "
                       "(`ui_proof capture-after` first)"]
    if criteria:
        data["criteria"] = criteria
    if no_visual:
        data["no_visual"] = no_visual
    for key, value in (("title", title), ("issue", issue)):
        if value:
            data[key] = value
    data.update({"state": state, "utc": store.utc_now()})
    page, failed = _write(repo_name, mr, folder, data)
    if page is None:
        return proof.MISSING, [f"PROOF_PAGE=not_built reason={failed}"]
    lines = [f"PROOF_PAGE={page}", f"BYTES={page.stat().st_size}"]
    if why:
        lines.append(f"DETAIL={why}")
    if not data.get("before"):
        lines.append("PROOF_WARNING=no before capture")
    return state, lines


def stamp(repo: Path, repo_name: str, mr: int, sha: str) -> Tuple[bool, List[str]]:
    """After the merge: put the merge sha on the built page and copy it to the primary checkout."""
    root = store.proof_root(repo)
    if root is None or not (root / str(mr) / store.PAGE_NAME).is_file():
        return False, [f"PUBLISHED=none reason=no .proof/{mr}/index.html in this checkout"]
    folder = root / str(mr)
    data = store.load_data(folder)
    data["merge_sha"] = sha
    page, failed = _write(repo_name, mr, folder, data)
    if page is None:
        return False, [f"PUBLISHED=none reason={failed}"]
    ok, line = store.publish(repo, mr)
    return ok, [f"PROOF_PAGE={page}", line]
