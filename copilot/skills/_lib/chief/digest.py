"""The phone-readable `board` and `sessions` digests. A count whose source
the launcher could not establish renders `?` with its reason, never `0`.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from . import rails


def _fmt_age(seconds: Any) -> str:
    """Compact age like `3m` / `2h` / `1d`. Unparseable input -> `?`."""
    try:
        secs = float(seconds)
    except (TypeError, ValueError):
        return "?"
    if secs < 0:
        return "?"
    if secs < 3600:
        return f"{int(secs // 60)}m"
    if secs < 86400:
        return f"{int(secs // 3600)}h"
    return f"{int(secs // 86400)}d"


# Each digest count token and the `/api/board` source section whose
# `available` flag says its column is real. An
# unreadable source hands back empty lists, and `len([])` is a plausible
# `0` — so a count is only ever rendered through `_source_unavailable`, and a
# new token cannot be added here without naming the source that establishes it.
# `other` mixes open PRs (github) with job cards, so a
# missing github fetch makes its total unknown too.
_DIGEST_COUNTS: Tuple[Tuple[str, str], ...] = (
    ("backlog", "github"),
    ("claude_turn", "live_sessions"),
    ("your_turn", "live_sessions"),
    ("other", "github"),
    ("done", "github"),
)


def _source_unavailable(board: Dict[str, Any], source: str) -> Optional[str]:
    """Why `board[source]`'s columns can't be counted, or None when they can.

    Absent (an older launcher build) is unknown, never "fetched" — only an
    explicit `available: true` establishes the column.
    """
    section = board.get(source)
    if not isinstance(section, dict) or "available" not in section:
        return "not reported by this launcher build"
    if section.get("available") is not True:
        error = section.get("error")
        if source == "github":
            if error:
                return f"no successful fetch yet, last attempt failed: {error}"
            return "never fetched in this launcher process"
        return str(error or "source unreadable")
    return None


def _format_rate_limit_line(board: Dict[str, Any]) -> str:
    """Claude's weekly window from `quota_lines` — `?` with the row's reason when
    there is no reading, never `None%`."""
    row = next(
        (line for line in board.get("quota_lines") or []
         if isinstance(line, dict) and line.get("harness") == "claude"),
        None,
    )
    if row is None:
        return "rate_limit_7d=? resets=? (no claude quota row reported)"
    weekly = row.get("weekly") or {}
    pct = weekly.get("used_percentage")
    if not isinstance(pct, (int, float)) or row.get("state") != "available":
        return f"rate_limit_7d=? resets=? ({row.get('state') or '?'}: {row.get('reason') or '?'})"
    line = f"rate_limit_7d={round(float(pct), 1)}% resets={weekly.get('resets_at') or '?'}"
    return line + " stale" if row.get("stale") else line


def format_board_digest(
    board: Dict[str, Any],
    fleet_jobs: Optional[Tuple[Optional[List[str]], Optional[str]]] = None,
    caller_sid: Optional[str] = None,
    grokbot_line: Optional[str] = None,
) -> str:
    """The ~12-line phone-readable digest: counts, live sessions, PR/job
    cards, running fleet-wide jobs, one rate-limit line, and the `grokbot=`
    token when the caller passes one (`board` does).

    A count whose source can't be established renders `?` and the next line
    says why — the token shape stays `key=value` so the chief's poll still
    parses it. `fleet_jobs` is `fetch_running_fleet_jobs`'s result; None
    (not fetched) omits that line.

    A session line is a worker the dispatch rails count. A standing
    `telegram:*` channel session is collapsed into one `channels` line, and
    the caller's own card (`caller_sid`, default the launcher session id in
    this process's env) is marked `(you, chief)`, so no line reads as an
    occupant that `rails.repo_occupancy` ignores."""
    cols = board.get("columns") or {}
    tokens: List[str] = []
    unknown: Dict[str, List[str]] = {}
    for column, source in _DIGEST_COUNTS:
        reason = _source_unavailable(board, source)
        values = cols.get(column)
        if reason is None and not isinstance(values, list):
            reason = f"column {column!r} missing from the payload"
        if reason is None:
            tokens.append(f"{column}={len(values)}")
        else:
            tokens.append(f"{column}=?")
            unknown.setdefault(f"{source}: {reason}", []).append(column)
    lines: List[str] = [" ".join(tokens)]
    if unknown:
        lines.append("unknown: " + " | ".join(
            f"{','.join(columns)} ({why})" for why, columns in unknown.items()
        ))
    github = board.get("github")
    if isinstance(github, dict) and github.get("available") is True and github.get("error"):
        lines.append(
            f"github: last fetch failed ({github.get('error')}); "
            f"counts are from {github.get('fetched_at')}"
        )

    me = caller_sid or rails.caller_session_id()
    channels: Dict[str, int] = {}
    for card in list(cols.get("claude_turn") or []) + list(cols.get("your_turn") or []):
        if rails._is_channel_session(card):
            repo = str(card.get("project") or "?")
            channels[repo] = channels.get(repo, 0) + 1
            continue
        full_sid = str(card.get("session_id") or "")
        you = " (you, chief)" if me and full_sid == me else ""
        lines.append(
            f"  {card.get('project')}: {card.get('status')} "
            f"age={_fmt_age(card.get('age_seconds'))} agent={card.get('agent')} "
            f"sid={full_sid[:8]}{you}"
        )
    if channels:
        lines.append(
            "channels (not workers, never block dispatch): "
            + ", ".join(f"{repo} x{n}" for repo, n in sorted(channels.items()))
        )

    for card in cols.get("other") or []:
        if card.get("kind") == "job":
            lines.append(f"  job {card.get('job_name')}: {card.get('state')}")
        else:
            lines.append(f"  PR {card.get('repo')}#{card.get('number')}: {card.get('title')}")

    if fleet_jobs is not None:
        running, error = fleet_jobs
        if running is None:
            lines.append(f"fleet jobs running: ? ({error})")
        else:
            lines.append(f"fleet jobs running: {', '.join(running) or 'none'}")
    lines.append(_format_rate_limit_line(board))
    if grokbot_line:
        lines.append(grokbot_line)
    return "\n".join(lines)


def format_occupancy(occupancy: Dict[str, Dict[str, Any]]) -> str:
    if not occupancy:
        return "(no repo currently occupied)"
    lines = []
    for repo, info in sorted(occupancy.items()):
        sid = str(info.get("session_id") or "")[:8]
        lines.append(
            f"{repo}: {info.get('status')} age={_fmt_age(info.get('age_seconds'))} "
            f"agent={info.get('agent')} sid={sid}"
        )
    return "\n".join(lines)
