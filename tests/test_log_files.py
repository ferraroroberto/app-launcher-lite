"""``src/log_files.py`` — shared file-log-handler bootstrap (issue #11).

Both webapp side logs (``webapp/auth.log``, ``webapp/slow-requests.log``) now
route through one helper, so the idempotence and never-raise properties that
each copy used to assert for itself are asserted here once.
"""

from __future__ import annotations

import logging

from src.log_files import LOG_FORMAT, ensure_file_log_handler


def _fresh_logger(name: str) -> logging.Logger:
    log = logging.getLogger(name)
    for handler in list(log.handlers):
        log.removeHandler(handler)
        handler.close()
    return log


def test_attaches_a_file_handler_at_the_requested_level(tmp_path):
    log = _fresh_logger("test_log_files.attach")
    path = tmp_path / "nested" / "side.log"

    ensure_file_log_handler(log, path, logging.WARNING)

    handlers = [h for h in log.handlers if isinstance(h, logging.FileHandler)]
    assert len(handlers) == 1
    assert handlers[0].level == logging.WARNING
    assert log.level == logging.WARNING
    assert handlers[0].formatter._fmt == LOG_FORMAT
    # The parent dir is created for us — webapp/ is gitignored runtime state
    # and may not exist on a fresh clone's first boot.
    assert path.parent.is_dir()

    log.warning("breadcrumb")
    handlers[0].flush()
    assert "breadcrumb" in path.read_text(encoding="utf-8")

    for handler in list(log.handlers):
        log.removeHandler(handler)
        handler.close()


def test_is_idempotent_across_repeated_boots(tmp_path):
    """``create_app()`` runs this on every boot and the logger outlives the
    app object — a second attach would double every line."""
    log = _fresh_logger("test_log_files.idempotent")
    path = tmp_path / "side.log"

    ensure_file_log_handler(log, path, logging.INFO)
    ensure_file_log_handler(log, path, logging.INFO)
    ensure_file_log_handler(log, path, logging.INFO)

    handlers = [h for h in log.handlers if isinstance(h, logging.FileHandler)]
    assert len(handlers) == 1

    for handler in list(log.handlers):
        log.removeHandler(handler)
        handler.close()


def test_unopenable_path_degrades_instead_of_raising(tmp_path, caplog):
    """A side log is a breadcrumb, not a startup dependency — an OSError here
    must not take the webapp's boot down with it."""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("i am a file", encoding="utf-8")
    log = _fresh_logger("test_log_files.unopenable")

    with caplog.at_level(logging.WARNING, logger="src.log_files"):
        ensure_file_log_handler(log, blocker / "side.log", logging.INFO)

    assert not [h for h in log.handlers if isinstance(h, logging.FileHandler)]
    assert any("Could not open" in r.getMessage() for r in caplog.records)
