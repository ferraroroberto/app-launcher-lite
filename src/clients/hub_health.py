"""The machine health verdict from the local hub, for the header's CPU reading.

The hub owns the measurement and the thresholds (its health watch samples the
ports in use, the memory commit and the disk latency on this machine). Lite only
reads ``GET /admin/api/machines/health`` over loopback and shows the verdict; it
never measures or judges the machine itself, because the computer it runs on may
be slow and must never get stuck on a reading.

**A request never waits on the hub.** :func:`build_sampler` wraps :func:`fetch`
in the background sampler of ``src/monitoring/listeners_sampler.py``: one fetch
at a time, on a pool of its own, spaced :data:`CACHE_SECONDS` after the last one
*finished* (a failed read waits as long as a good one, so a down hub costs one
bounded connect per half minute), and only while the page asks. The route reads
the cached answer.

**Unknown is its own state.** A hub that is not configured, does not answer,
answers with something that is not a verdict, itself reports ``unknown``, or a
read that has not finished (``warming``), ran past its budget (``overrun``),
raised (``error``) or went stale (``stale``) reads ``unknown`` with a ``why``;
none of them ever reads ``ok``.

A transition into or out of ``unknown`` leaves one breadcrumb on the
``launcher.slowreq`` logger, which persists to ``webapp/slow-requests.log``: the
live webapp's own stdout is discarded by the tray.
"""

from __future__ import annotations

import ipaddress
import logging
import time
from typing import Any, Callable, Dict, Iterable, List, Optional
from urllib.parse import urlparse

import requests

from src.clients import _loopback_http
from src.monitoring.listeners_sampler import ListenersSampler
from src.request_timing import TimedExecutor

breadcrumbs = logging.getLogger("launcher.slowreq")

VERDICTS = ("ok", "watch", "act")
VERDICT_UNKNOWN = "unknown"

# Gap between the end of one fetch and the start of the next, a good answer or not.
CACHE_SECONDS = 30.0
# A fetch running longer than this reads ``overrun``; the HTTP timeout below ends
# it first, so this only fires when the transport itself wedges.
FETCH_BUDGET_SECONDS = 5.0
# The hub answers from memory, so a healthy one answers in milliseconds; past
# this it is down or wedged, and the page must not wait for it.
_TIMEOUT = 3.0
# A reason or fix is one line for a sheet row; the hub's own text is short, this
# only keeps a surprising payload from filling the sheet.
_MAX_TEXT = 240
_MAX_HOLDERS = 5
_MAX_MEMORY_OFFENDERS = 3
_MAX_DISKS = 8
_MAX_DRIVES = 4
# The hub's per-disk states; anything else reads unknown, never ok.
_DISK_STATES = frozenset({"ok", "watch", "alert", "unknown"})

# What the Settings button "Use the local hub" fills in: where hub-lite listens.
# port-host-exempt: hub-lite's documented address, offered as a convenience; the saved value lives in the ignored config
LOCAL_HUB_URL = "http://127.0.0.1:8020"

# The sampler's own thread: a slow or hung hub stalls only this reading.
POOL = TimedExecutor("hub-health-sample", max_workers=1)


class HubHealthError(_loopback_http.LoopbackError):
    """Raised when the hub is unreachable or answers with an error."""


def _unknown(why: str, reason: Optional[str] = None) -> Dict[str, Any]:
    return {"verdict": VERDICT_UNKNOWN, "why": why, "reason": reason, "fix": None}


def unavailable(reason: str, detail: str) -> Dict[str, Any]:
    """The reading for a sampler state that holds no verdict (``warming``,
    ``overrun``, ``stale``, ``error``), with the sampler's own detail."""
    return _unknown(reason, detail or None)


def _text(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return None
    return " ".join(value.split())[:_MAX_TEXT]


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _rows(rows: Any, limit: int, keys: Iterable[str]) -> List[Dict[str, Any]]:
    """The first ``limit`` process rows with only ``keys``, text trimmed."""
    out: List[Dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or len(out) >= limit:
            continue
        out.append({k: _text(row.get(k)) if k == "name" else _num(row.get(k)) for k in keys})
    return out


def _disks(body: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
    """One row per physical disk the hub measured, or ``None`` when the payload
    has no usable ``disks`` block (an older hub, or a malformed one): the sheet
    then shows no disk rows, never a healthy-looking empty list. A row whose
    state is missing or unrecognised reads ``unknown``; a reading that is not a
    number is ``None``, never 0."""
    rows = body.get("disks")
    if not isinstance(rows, list):
        return None
    out: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or len(out) >= _MAX_DISKS:
            continue
        drives = [d for d in (_text(x) for x in (row.get("drives") if isinstance(row.get("drives"), list) else []))
                  if d][:_MAX_DRIVES]
        disk = _text(row.get("disk"))
        if not drives and not disk:
            continue
        state = row.get("state")
        out.append({
            "disk": disk,
            "drives": drives,
            "state": state if state in _DISK_STATES else "unknown",
            "busy_percent": _num(row.get("busy_percent")),
            "queue": _num(row.get("queue")),
            "read_ms": _num(row.get("read_ms")),
            "write_ms": _num(row.get("write_ms")),
        })
    return out or None


def _machine(body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """What the machine rows draw beyond the verdict: the worst ports percent,
    commit percent, the port holders, the last exhaustion event and the per-disk
    load. ``None`` when the payload carries none of them. A stale or missing port
    block leaves ``ports_percent`` unset, never 0."""
    system = body.get("system") if isinstance(body.get("system"), dict) else {}
    ports = body.get("ports") if isinstance(body.get("ports"), dict) else {}
    percents = [
        _num(ports[proto].get("percent")) for proto in ("tcp", "udp")
        if ports.get("stale") is not True and isinstance(ports.get(proto), dict)
    ]
    percents = [p for p in percents if p is not None]
    offenders = body.get("offenders") if isinstance(body.get("offenders"), dict) else {}
    holders = _rows(ports.get("top_holders") or offenders.get("ports"), _MAX_HOLDERS,
                    ("name", "pid", "value", "tcp", "udp"))
    event = ports.get("last_event") if isinstance(ports.get("last_event"), dict) else None
    disks = _disks(body)
    machine = {
        "host": _text(body.get("host_id")),
        "ports_percent": max(percents) if percents else None,
        "commit_percent": _num(system.get("commit_percent")),
        "holders": holders,
        "memory_holders": _rows(offenders.get("memory_mb"), _MAX_MEMORY_OFFENDERS, ("name", "pid", "value")),
        "last_event": None if event is None else {
            "ts": _num(event.get("ts")), "event_id": _num(event.get("event_id")),
            "protocol": _text(event.get("protocol")), "reason": _text(event.get("reason")),
        },
        "disks": disks,
    }
    measured = (machine["ports_percent"], machine["commit_percent"], machine["last_event"], disks)
    if all(v is None for v in measured) and not holders:
        return None
    return machine


def parse(body: Any) -> Dict[str, Any]:
    """The hub's payload as Lite shows it. Anything that is not a recognised
    verdict is ``unknown``, never ``ok``."""
    if not isinstance(body, dict):
        return _unknown("unreadable")
    verdict = body.get("verdict")
    if verdict in VERDICTS:
        out: Dict[str, Any] = {
            "verdict": verdict,
            "why": None,
            "reason": _text(body.get("reason")),
            "fix": _text(body.get("fix")),
        }
        machine = _machine(body)
        if machine is not None:
            out["machine"] = machine
        return out
    if verdict == VERDICT_UNKNOWN:
        return _unknown("hub", _text(body.get("reason")))
    return _unknown("unreadable")


def checked_url(raw: Any) -> str:
    """The hub base URL as saved, or ``ValueError`` saying what is wrong.

    Empty means no hub. Otherwise: ``http`` or ``https``, a host that is this PC
    (``localhost`` or a loopback address) or a private-network address, an optional
    port and nothing after it. The hub is sent job output and read for machine
    health, so a public name is refused rather than trusted. Checked on write
    only: a file edited by hand keeps loading whatever it holds.
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        parsed = urlparse(text)
        parsed.port  # noqa: B018 — reading it is the check: a non-numeric port raises
        host = parsed.hostname or ""
    except ValueError:
        raise ValueError(f"{text!r} is not a valid URL; use the form http://localhost:PORT") from None
    if parsed.scheme not in ("http", "https") or not host:
        raise ValueError("The hub URL must start with http:// or https:// and name a host and port, e.g. http://localhost:PORT")
    if parsed.path not in ("", "/") or parsed.params or parsed.query or parsed.fragment or parsed.username is not None:
        raise ValueError("The hub URL is the address only (scheme, host and port): drop the path, query and login")
    if not _is_local_host(host):
        raise ValueError(f"{host} is not on this PC or the private network; use localhost or a private-network IP address")
    return f"{parsed.scheme}://{parsed.netloc}"


def _is_local_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return addr.is_loopback or addr.is_private


def check_answers(base_url: str) -> None:
    """Probe ``<base>/healthz`` once; raises :class:`HubHealthError` with the
    reason in plain words when nothing there answers. Blocking: a route runs it
    on the outbound pool."""
    url = f"{base_url.rstrip('/')}/healthz"
    try:
        resp = _loopback_http.pooled_request("GET", url, timeout=_TIMEOUT, retry_dropped=False)
    except requests.exceptions.SSLError:
        raise HubHealthError(f"{base_url} answered but its certificate could not be verified", status=400) from None
    except requests.exceptions.Timeout:
        raise HubHealthError(f"{base_url} did not answer within {_TIMEOUT:.0f} s", status=400) from None
    except requests.exceptions.ConnectionError:
        raise HubHealthError(f"Nothing is listening at {base_url}: the connection was refused or the host is not there", status=400) from None
    except requests.RequestException as exc:
        raise HubHealthError(f"{base_url} could not be reached ({type(exc).__name__})", status=400) from None
    if resp.status_code >= 400:
        raise HubHealthError(f"{base_url} answered HTTP {resp.status_code} at /healthz, so it does not look like the hub", status=400)


def health_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/admin/api/machines/health"


def fetch(base_url: str) -> Dict[str, Any]:
    """One bounded read of the hub; never raises. Blocking: only the sampler's
    pool calls it, a request never does."""
    base = (base_url or "").strip()
    if not base:
        return _unknown("disabled")
    try:
        body = _loopback_http.request(
            "GET", health_url(base), error=HubHealthError, service="local-llm-hub",
            timeout=_TIMEOUT, allow_empty=False,
        )
    except HubHealthError as exc:
        return _unknown("unreachable", str(exc))
    return parse(body)


class _Reader:
    """The sampler's scan: one fetch of the configured hub, with the transition breadcrumbs."""

    def __init__(self, base_url: Callable[[], str]) -> None:
        self._base_url = base_url
        self._was_unknown = False

    def scan(self) -> Dict[str, Any]:
        reading = fetch(self._base_url())
        if reading.get("why") != "disabled":
            self._note_transition(reading)
        return reading

    def _note_transition(self, reading: Dict[str, Any]) -> None:
        now_unknown = reading["verdict"] == VERDICT_UNKNOWN
        if now_unknown and not self._was_unknown:
            breadcrumbs.info("⚠️ hub health unknown (%s): %s", reading.get("why"), reading.get("reason") or "no detail")
        elif self._was_unknown and not now_unknown:
            breadcrumbs.info("✅ hub health readable again: %s", reading["verdict"])
        self._was_unknown = now_unknown


def build_sampler(
    base_url: Callable[[], str],
    *,
    executor: Any = POOL,
    clock: Callable[[], float] = time.monotonic,
) -> ListenersSampler[Dict[str, Any]]:
    """The background sampler behind ``/api/system/health``. ``base_url`` is read
    at each fetch."""
    return ListenersSampler(
        _Reader(base_url).scan,
        executor=executor,
        clock=clock,
        tick_s=CACHE_SECONDS,
        budget_s=FETCH_BUDGET_SECONDS,
        max_age_s=3 * CACHE_SECONDS,
        label="hub health",
    )
