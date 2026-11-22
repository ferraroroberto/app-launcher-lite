"""`health` subcommand: one read-only snapshot of how loaded this PC and Lite are.

Writes ``webapp/health/health-<date>-<time>.txt`` (listeners, top CPU, agent
processes, lane count, memory, request pressure, the slow-request tail). It
signals no process and starts no work; see :mod:`src.health_snapshot`.
"""

from __future__ import annotations

import argparse
import logging

from src import diagnostics_bundle, health_snapshot
from src.webapp_config import load_webapp_config

from .base import BaseCommand

logger = logging.getLogger(__name__)


class HealthCommand(BaseCommand):
    @classmethod
    def add_parser(cls, subparsers: argparse._SubParsersAction) -> None:
        subparsers.add_parser(
            "health",
            help="Write a read-only health snapshot (load, agents, lanes, slow requests)",
        )

    def execute(self, args: argparse.Namespace) -> int:
        cfg = load_webapp_config()
        try:
            snap = health_snapshot.save_snapshot(
                diagnostics_bundle.sources_from_config(cfg),
                literals=tuple(diagnostics_bundle.configured_secrets(cfg)),
            )
        except OSError as exc:
            logger.error(f"❌ health snapshot not saved: {exc}")
            return 1
        logger.info(f"✅ Saved health snapshot to {snap.path}")
        return 0
