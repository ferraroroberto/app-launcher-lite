"""`diagnostics` subcommand: save the last N minutes of every Lite log.

The command-line twin of Settings > Diagnostics, for a PC whose webapp is
down or unreachable: it reads the same logs and state files and writes the
same redacted file under ``webapp/diagnostics/``.
"""

from __future__ import annotations

import argparse
import logging

from src import diagnostics_bundle
from src.webapp_config import load_webapp_config

from .base import BaseCommand

logger = logging.getLogger(__name__)


class DiagnosticsCommand(BaseCommand):
    @classmethod
    def add_parser(cls, subparsers: argparse._SubParsersAction) -> None:
        p = subparsers.add_parser(
            "diagnostics",
            help="Save the last N minutes of every Lite log to one redacted file",
        )
        p.add_argument(
            "--minutes",
            type=int,
            default=60,
            help=f"How far back to collect, 1 to {diagnostics_bundle.MAX_WINDOW_MINUTES} (default 60)",
        )

    def execute(self, args: argparse.Namespace) -> int:
        cfg = load_webapp_config()
        try:
            saved = diagnostics_bundle.save_bundle(
                diagnostics_bundle.sources_from_config(cfg),
                args.minutes,
                literals=diagnostics_bundle.configured_secrets(cfg),
            )
        except (OSError, ValueError) as exc:
            logger.error(f"❌ diagnostics not saved: {exc}")
            return 1
        logger.info(f"✅ Saved {saved.records} log records to {saved.path}")
        return 0
