"""`webapp` subcommand — run the FastAPI server in the foreground."""

from __future__ import annotations

import argparse
import logging

from .base import BaseCommand

logger = logging.getLogger(__name__)


class WebappCommand(BaseCommand):
    @classmethod
    def add_parser(cls, subparsers: argparse._SubParsersAction) -> None:
        p = subparsers.add_parser(
            "webapp",
            help="Run the FastAPI webapp in the foreground (no tray)",
        )
        p.add_argument("--host", default=None, help="Override host")
        p.add_argument("--port", type=int, default=None, help="Override port")

    def execute(self, args: argparse.Namespace) -> int:
        # Lazy imports so a plain `webapp` subcommand doesn't drag pystray in.
        from app.webapp import serve
        from app.webapp.event_loop import selector_loop_factory
        from app.webapp.manager import current_serve_plan
        from app.webapp.serve_modes import add_http_port
        from src.webapp_config import load_webapp_config

        cfg = load_webapp_config()
        host = args.host or cfg.host
        port = args.port or cfg.port

        plan = current_serve_plan()
        if plan.warning:
            logger.warning(f"⚠️ {plan.warning}")
        kwargs = {"log_level": "info", "loop_factory": selector_loop_factory}
        if plan.certs is not None:
            cert, key = plan.certs
            kwargs["certfile"] = str(cert)
            kwargs["keyfile"] = str(key)
        if plan.http_port is not None:
            add_http_port(plan.http_port)
            logger.info(f"ℹ️ plain HTTP also on port {plan.http_port}")

        logger.info(
            f"🚀 launcher webapp on {plan.scheme}://{host if host != '0.0.0.0' else '127.0.0.1'}:{port}"
        )
        serve.run(host, port, **kwargs)
        return 0
