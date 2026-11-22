"""CLI subcommand registry."""

from .base import BaseCommand
from .diagnostics_cmd import DiagnosticsCommand
from .health_cmd import HealthCommand
from .run_job_cmd import RunJobCommand
from .scan_cmd import ScanCommand
from .session_host_cmd import SessionHostCommand
from .tray_cmd import TrayCommand
from .webapp_cmd import WebappCommand

COMMANDS = {
    "tray": TrayCommand,
    "webapp": WebappCommand,
    "scan": ScanCommand,
    "session-host": SessionHostCommand,
    "run-job": RunJobCommand,
    "diagnostics": DiagnosticsCommand,
    "health": HealthCommand,
}


def get_command(name: str):
    return COMMANDS.get(name)


__all__ = ["BaseCommand", "COMMANDS", "get_command"]
