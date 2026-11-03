"""POST /api/login lock-out and guessing alert.

Time is a fake clock (no sleeping) and the notifier is a fake, so nothing
here waits or sends a real Telegram message.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import List

from fastapi.testclient import TestClient

from app.webapp.routers import auth as auth_router
from src.login_lockout import LoginLockout
from src.notify import TelegramNotifier

PASSWORD = "hunter2"
TOKEN = "secret-token"
APP_NAME = "App Launcher Lite"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: List[str] = []
        self.threads: List[int] = []

    def send_text(self, text: str) -> None:
        self.sent.append(text)
        self.threads.append(threading.get_ident())


def _arm(app, clock: FakeClock, notifier: FakeNotifier | None = None, *, spawn=None):
    """Replace the app's lock-out with one on the fake clock (and fake notifier)."""
    app.state.webapp_config.auth_password = PASSWORD
    app.state.webapp_config.auth_token = TOKEN
    on_alert = None
    if notifier is not None:
        on_alert = auth_router.make_login_alert(
            lambda: app.state.webapp_config,
            build=lambda _cfg: notifier,
            spawn=spawn or (lambda fn: fn()),
        )
    app.state.login_lockout = LoginLockout(clock=clock, on_alert=on_alert)


def _wrong(client, **kw):
    return client.post("/api/login", json={"password": "wrong"}, **kw)


def _right(client, **kw):
    return client.post("/api/login", json={"password": PASSWORD}, **kw)


class TestLockout:
    def test_429_with_retry_after_after_repeated_wrong_passwords(self, webapp_client):
        client, app, _ = webapp_client
        clock = FakeClock()
        _arm(app, clock)

        # The first five wrong passwords are free; the sixth starts the wait.
        assert [_wrong(client).status_code for _ in range(6)] == [401] * 6
        locked = _wrong(client)
        assert locked.status_code == 429
        assert int(locked.headers["Retry-After"]) >= 1

        # Still locked mid-wait, and the refusal did not extend it.
        clock.advance(1)
        assert _right(client).status_code == 429

        clock.advance(int(locked.headers["Retry-After"]))
        ok = _right(client)
        assert ok.status_code == 200
        assert ok.json() == {"token": TOKEN}

    def test_correct_password_clears_the_streak(self, webapp_client):
        client, app, _ = webapp_client
        _arm(app, FakeClock())
        for _ in range(5):
            assert _wrong(client).status_code == 401
        assert _right(client).status_code == 200
        for _ in range(5):
            assert _wrong(client).status_code == 401

    def test_pc_itself_is_never_locked_out(self, webapp_client):
        _, app, _ = webapp_client
        _arm(app, FakeClock())
        pc = TestClient(app, client=("127.0.0.1", 50000))
        assert [_wrong(pc).status_code for _ in range(12)] == [401] * 12
        assert _right(pc).status_code == 200

    def test_tunnel_visitor_over_loopback_is_not_the_pc(self, webapp_client):
        _, app, _ = webapp_client
        clock = FakeClock()
        _arm(app, clock)
        tunnel = TestClient(app, client=("127.0.0.1", 50000))
        visitor = {"cf-ray": "abc", "cf-connecting-ip": "203.0.113.9"}
        other = {"cf-ray": "abc", "cf-connecting-ip": "203.0.113.10"}

        assert [_wrong(tunnel, headers=visitor).status_code for _ in range(6)] == [401] * 6
        assert _wrong(tunnel, headers=visitor).status_code == 429
        # Keyed per visitor: another address is untouched, and so is the PC.
        assert _wrong(tunnel, headers=other).status_code == 401
        assert _wrong(tunnel).status_code == 401

    def test_edge_header_from_a_non_loopback_socket_is_ignored(self, webapp_client):
        client, app, _ = webapp_client
        _arm(app, FakeClock())
        for i in range(6):
            assert _wrong(client, headers={"cf-connecting-ip": f"198.51.100.{i}"}).status_code == 401
        # Rotating the header buys nothing: the key is the socket address.
        assert _wrong(client, headers={"cf-connecting-ip": "198.51.100.99"}).status_code == 429

    def test_unparseable_edge_header_falls_back_to_the_socket(self, webapp_client):
        _, app, _ = webapp_client
        _arm(app, FakeClock())
        tunnel = TestClient(app, client=("127.0.0.1", 50000))
        junk = {"cf-ray": "abc", "cf-connecting-ip": "not-an-address"}
        assert [_wrong(tunnel, headers=junk).status_code for _ in range(6)] == [401] * 6
        assert _wrong(tunnel, headers=junk).status_code == 429


class TestAlert:
    def test_three_wrong_passwords_send_exactly_one_alert(self, webapp_client):
        client, app, _ = webapp_client
        notifier = FakeNotifier()
        _arm(app, FakeClock(), notifier)

        for _ in range(2):
            _wrong(client)
        assert notifier.sent == []
        _wrong(client)
        assert len(notifier.sent) == 1

        text = notifier.sent[0]
        assert APP_NAME in text
        assert "testclient" in text
        assert "3" in text
        assert PASSWORD not in text and TOKEN not in text

        # Further failures in the same streak raise no second alert.
        for _ in range(4):
            _wrong(client)
        assert len(notifier.sent) == 1

    def test_tunnel_alert_names_the_visitor_not_loopback(self, webapp_client):
        _, app, _ = webapp_client
        notifier = FakeNotifier()
        _arm(app, FakeClock(), notifier)
        tunnel = TestClient(app, client=("127.0.0.1", 50000))
        for _ in range(3):
            _wrong(tunnel, headers={"cf-ray": "abc", "cf-connecting-ip": "203.0.113.9"})
        assert len(notifier.sent) == 1
        assert "203.0.113.9" in notifier.sent[0]
        assert "127.0.0.1" not in notifier.sent[0]

    def test_pc_typos_raise_no_alert(self, webapp_client):
        _, app, _ = webapp_client
        notifier = FakeNotifier()
        _arm(app, FakeClock(), notifier)
        pc = TestClient(app, client=("127.0.0.1", 50000))
        for _ in range(6):
            _wrong(pc)
        assert notifier.sent == []

    def test_send_runs_off_the_request_thread(self, webapp_client):
        client, app, _ = webapp_client
        notifier = FakeNotifier()
        sent = threading.Event()
        original = notifier.send_text

        def send_text(text: str) -> None:
            original(text)
            sent.set()

        notifier.send_text = send_text  # type: ignore[method-assign]
        _arm(app, FakeClock(), notifier, spawn=auth_router._start_thread)
        for _ in range(3):
            _wrong(client)
        assert sent.wait(timeout=5)
        assert notifier.threads[0] != threading.get_ident()

    def test_failing_notifier_never_breaks_the_login(self, webapp_client):
        client, app, _ = webapp_client
        notifier = FakeNotifier()

        def boom(text: str) -> None:
            raise auth_router.NotifierError("down")

        notifier.send_text = boom  # type: ignore[method-assign]
        _arm(app, FakeClock(), notifier)
        assert [_wrong(client).status_code for _ in range(3)] == [401] * 3


class TestAlertNotifier:
    def _cfg(self, **kw):
        base = dict(telegram_bot_token="tok", telegram_chat_id="", telegram_alert_chat_id="")
        return SimpleNamespace(**{**base, **kw})

    def test_none_without_bot_or_chat(self):
        assert auth_router._build_alert_notifier(self._cfg(telegram_bot_token="")) is None
        assert auth_router._build_alert_notifier(self._cfg()) is None

    def test_attention_chat_wins_over_the_per_job_chat(self):
        notifier = auth_router._build_alert_notifier(
            self._cfg(telegram_chat_id="jobs", telegram_alert_chat_id="attention")
        )
        assert isinstance(notifier, TelegramNotifier)
        assert notifier._chat_id == "attention"

    def test_falls_back_to_the_per_job_chat(self):
        notifier = auth_router._build_alert_notifier(self._cfg(telegram_chat_id="jobs"))
        assert isinstance(notifier, TelegramNotifier)
        assert notifier._chat_id == "jobs"
