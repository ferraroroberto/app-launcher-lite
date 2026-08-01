"""Failure notifications for Jobs-tab runs (issue #66, issue #597).

A small protocol surface + concrete Pushover/Telegram implementations;
the executor (:mod:`app.cli.commands.run_job_cmd`) calls
:func:`build_notifier_from_config` / :func:`build_telegram_notifier_from_config`
on finalisation and pushes when the run failed (or, optionally, when an
N-failure streak ticks over).

Two independent channels share this one finalisation hook:

* **Pushover (global, issue #66)** — every job, gated by the master
  switch. Config keys live in :class:`src.webapp_config.WebappConfig`:

  * ``pushover_api_token`` / ``pushover_user_key`` — credentials.
    Missing creds → :class:`NoopNotifier`.
  * ``notify_on_failure`` — master switch (default off, so the feature
    ships dormant until the user opts in).
  * ``notify_failure_streak`` — extra fire when the consecutive-failure
    count equals this value (0 = disabled).

* **Telegram (per-job, issue #597)** — only jobs with
  ``Job.alert_on_failure = True``, via the vendored :mod:`src.notify`
  Telegram primitive. Config keys:

  * ``telegram_bot_token`` / ``telegram_chat_id`` — credentials.
    Missing creds → :class:`NoopNotifier`.

  Opt-in per job (default off) so the shared Telegram chat isn't
  spammed by every job's failures.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

import requests

from src.notify import NotifierError as TelegramNotifierError
from src.notify import TelegramNotifier as _VendoredTelegramNotifier

logger = logging.getLogger(__name__)

PUSHOVER_URL = "https://api.pushover.net/1/messages.json"


class Notifier(Protocol):
    """Minimal push-notification surface — see :class:`PushoverNotifier`."""

    def notify(self, title: str, body: str, severity: str) -> None: ...


class NoopNotifier:
    """No-op notifier — used when credentials are not configured."""

    def notify(self, title: str, body: str, severity: str) -> None:
        return None


class PushoverNotifier:
    """POST to Pushover. Errors are logged and swallowed.

    ``severity`` maps to Pushover ``priority``:

    * ``"info"``    →  -1 (low/no sound)
    * ``"warning"`` →   0 (normal)
    * ``"error"``   →   1 (high — bypass quiet hours)
    """

    _PRIORITY = {"info": -1, "warning": 0, "error": 1}

    def __init__(
        self,
        api_token: str,
        user_key: str,
        *,
        http: Any = None,
        timeout_seconds: float = 8.0,
    ) -> None:
        self._api_token = api_token
        self._user_key = user_key
        self._http = http or requests
        self._timeout_seconds = timeout_seconds

    def notify(self, title: str, body: str, severity: str = "warning") -> None:
        # Pushover caps message length at ~1024 chars; truncate so the
        # tail of long failures doesn't get dropped by Pushover itself.
        max_message = 1024
        message = body if len(body) <= max_message else body[: max_message - 1] + "…"
        payload = {
            "token": self._api_token,
            "user": self._user_key,
            "title": title[:250],
            "message": message,
            "priority": self._PRIORITY.get(severity, 0),
        }
        try:
            resp = self._http.post(
                PUSHOVER_URL, data=payload, timeout=self._timeout_seconds
            )
            if not (200 <= resp.status_code < 300):
                logger.warning(
                    f"⚠️  pushover non-2xx: rc={resp.status_code} "
                    f"body={resp.text[:200]!r}"
                )
        except Exception as exc:  # noqa: BLE001 — exec-side: never raise
            logger.warning(f"⚠️  pushover send failed: {exc}")


class TelegramNotifier:
    """Adapts the vendored :class:`src.notify.TelegramNotifier` to the
    ``Notifier`` protocol used by the executor (issue #597).

    ``title`` and ``body`` are joined into one plain-text message —
    Telegram has no separate subject line. Errors are logged and
    swallowed, same contract as :class:`PushoverNotifier`.
    """

    def __init__(self, bot_token: str, chat_id: str) -> None:
        self._notifier = _VendoredTelegramNotifier(bot_token, chat_id)

    def notify(self, title: str, body: str, severity: str = "warning") -> None:
        try:
            self._notifier.send_text(f"{title}\n\n{body}" if body else title)
        except TelegramNotifierError as exc:
            logger.warning(f"⚠️  telegram send failed: {exc}")


def build_notifier_from_config(cfg: Any) -> Notifier:
    """Construct a Notifier from a :class:`WebappConfig`-shaped object.

    Returns :class:`NoopNotifier` when creds or the master switch are
    missing — every caller can unconditionally ``notifier.notify(...)``.
    """
    api_token = getattr(cfg, "pushover_api_token", "") or ""
    user_key = getattr(cfg, "pushover_user_key", "") or ""
    if not (api_token and user_key):
        return NoopNotifier()
    return PushoverNotifier(api_token, user_key)


def build_telegram_notifier_from_config(cfg: Any) -> Notifier:
    """Construct a Telegram :class:`Notifier` from a :class:`WebappConfig`-shaped object.

    Returns :class:`NoopNotifier` when either credential is missing —
    every caller can unconditionally ``notifier.notify(...)``.
    """
    bot_token = getattr(cfg, "telegram_bot_token", "") or ""
    chat_id = getattr(cfg, "telegram_chat_id", "") or ""
    if not (bot_token and chat_id):
        return NoopNotifier()
    return TelegramNotifier(bot_token, chat_id)
