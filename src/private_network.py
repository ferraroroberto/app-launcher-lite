"""This PC's private IPv4 address, for the PC load sheet's IP row.

Discovery is best effort and kept apart from auth, terminal reachability and
load sampling: it only answers "what address could a second device on the
network open this launcher at?".

Two ways to find it, in order:

1. **The active route.** A UDP ``connect`` sends no packet: it makes the OS pick
   the interface it would route through, whose address ``getsockname`` reports.
2. **The interfaces.** When there is no route (a LAN with no default gateway),
   the addresses the host name resolves to; the first private one wins.

An address outside the private ranges (a loopback, link-local or public one) is
not what a second device could use, so it never counts as found.

The last good address is cached in this module. A failed or empty re-read
keeps it, so the sheet never loses an address it already had.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import threading
from typing import List, Optional

_log = logging.getLogger("launcher.private_network")

_PRIVATE_RANGES = tuple(
    ipaddress.ip_network(net)
    for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)

# Any address works: connecting a UDP socket transmits nothing, and a private
# destination keeps the OS from preferring a VPN or tunnel adapter's route.
# port-host-exempt: an unroutable address used only to read the LAN route, never reached
_ROUTE_PROBE = ("10.255.255.255", 9)

_lock = threading.Lock()
_cached: Optional[str] = None


def _usable(address: str) -> bool:
    """True for an RFC 1918 IPv4 address another device could reach."""
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return parsed.version == 4 and any(parsed in net for net in _PRIVATE_RANGES)


def _route_address() -> Optional[str]:
    """The address of the interface the OS would route outbound through."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect(_ROUTE_PROBE)
        return probe.getsockname()[0]


def _interface_addresses() -> List[str]:
    """Every IPv4 address this host name resolves to."""
    infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    return [str(info[4][0]) for info in infos]


def discover() -> Optional[str]:
    """This PC's private IPv4 address, or ``None`` when none can be found.

    Never raises: each source that fails is logged and the next one tried.
    """
    try:
        address = _route_address()
        if address and _usable(address):
            return address
        _log.info("ℹ️ private ip: the active route gave no private address (%s)", address)
    except OSError as exc:
        _log.info("ℹ️ private ip: no active route (%s)", exc)
    try:
        for address in _interface_addresses():
            if _usable(address):
                return address
    except OSError as exc:
        _log.info("ℹ️ private ip: could not list the interfaces (%s)", exc)
    return None


def refresh() -> bool:
    """Re-read the address and cache it; ``True`` when discovery found one.

    ``False`` (nothing found, or discovery raised) keeps the cached address.
    Blocking, so callers on the event loop run it in a thread.
    """
    global _cached
    try:
        address = discover()
    except Exception as exc:  # noqa: BLE001 - startup and the sheet must never fail on this
        _log.warning("⚠️ private ip discovery failed: %s", exc)
        return False
    if address is None:
        return False
    with _lock:
        _cached = address
    return True


def current() -> Optional[str]:
    """The cached address; ``None`` until a discovery has found one."""
    with _lock:
        return _cached


def reset() -> None:
    """Forget the cached address (tests)."""
    global _cached
    with _lock:
        _cached = None
