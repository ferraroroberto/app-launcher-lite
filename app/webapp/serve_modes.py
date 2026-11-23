"""Which scheme the webapp is served over, and the launch that serves HTTPS and HTTP at once.

Settings > Connections > Serve over (``serve_over`` in ``webapp_config.json``)
picks one of four modes, read at each start:

- ``auto``: HTTPS when ``webapp/certificates/`` holds a pair, HTTP otherwise.
  This is how Lite served before the setting existed, so it is the default.
- ``https``: HTTPS. With no pair yet it still serves HTTP, with a warning,
  so the app stays reachable to create one.
- ``http``: never TLS, even when a pair exists.
- ``both``: HTTPS on the webapp port plus plain HTTP on ``serve_http_port``.
  HTTP and HTTPS cannot share one port.

A phone's browser only opens the microphone on an HTTPS page, which is why
the choice exists at all.

``app.webapp.serve`` is vendored byte-for-byte and binds one address, so
``both`` launches through this module instead::

    python -m app.webapp.serve_modes --http-port N <every app.webapp.serve flag>

It adds hypercorn's ``insecure_bind`` to the config the vendored module builds
and then runs that module unchanged, so every other server setting (HTTP/2,
loop, proxy trust, ping interval, the h2 stream release) stays the same.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Tuple

from app.webapp import serve

logger = logging.getLogger(__name__)

SERVE_MODES_MODULE = "app.webapp.serve_modes"


@dataclass(frozen=True)
class ServePlan:
    """What one start serves: TLS files when HTTPS, and the extra HTTP port in ``both``."""

    certs: Optional[Tuple[Path, Path]]
    http_port: Optional[int] = None
    # Set when the chosen mode cannot be served as asked (no certificate yet).
    warning: Optional[str] = None

    @property
    def scheme(self) -> str:
        return "https" if self.certs else "http"


def serve_plan(mode: str, http_port: int, certs: Optional[Tuple[Path, Path]]) -> ServePlan:
    """The plan for ``mode`` given the pair ``cert_paths()`` found (``None`` when there is none)."""
    if mode == "http":
        return ServePlan(certs=None)
    if mode in ("https", "both") and certs is None:
        return ServePlan(
            certs=None,
            warning=(
                f"serve_over is {mode!r} but webapp/certificates/ has no cert.pem + key.pem; "
                "serving HTTP until one is created (Settings > Connections > Serve over)"
            ),
        )
    if mode == "both":
        return ServePlan(certs=certs, http_port=http_port)
    return ServePlan(certs=certs)


def add_http_port(http_port: int) -> None:
    """Make this process's ``serve.run`` also bind plain HTTP on ``http_port`` beside TLS.

    Hypercorn binds ``insecure_bind`` only when TLS is on; without it the one
    ``bind`` is plain HTTP already.
    """
    build = serve.build_config

    def build_config(host: str, port: int, *args: Any, **kwargs: Any) -> Any:
        cfg = build(host, port, *args, **kwargs)
        cfg.insecure_bind = [f"{host}:{http_port}"]
        return cfg

    serve.build_config = build_config  # type: ignore[assignment]


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog=f"python -m {SERVE_MODES_MODULE}", add_help=False)
    ap.add_argument("--http-port", type=int, required=True)
    own, rest = ap.parse_known_args(argv)
    if "--ssl-certfile" not in rest or serve.UVICORN in rest:
        # Without TLS hypercorn binds `bind` as plain HTTP and ignores
        # insecure_bind; the uvicorn fallback binds one address only.
        logger.warning("⚠️ serve_over 'both' needs TLS and hypercorn; serving the webapp port only")
    else:
        add_http_port(own.http_port)
    return serve.main(rest)


if __name__ == "__main__":
    sys.exit(main())
