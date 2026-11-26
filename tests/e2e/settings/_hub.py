"""Settings > Local hub, walked inside the Settings sheets test: a bad address is
refused in words and saves nothing, a hub that answers is saved and read back
by the row, and clearing it turns the hub off, all without a reload."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterator

from playwright.sync_api import Page, expect

from tests.e2e.fixtures.pages import loopback
from tests.e2e.fixtures.sessions import auth_headers
from tests.e2e.fixtures.ui import close_settings_sheets, open_settings_sheet


@contextmanager
def _fake_hub() -> Iterator[str]:
    """A loopback server that answers GET /healthz, standing in for the hub."""
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 — the http.server hook name
            ok = self.path == "/healthz"
            self.send_response(200 if ok else 404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _saved(base_url: str, auth_token: str) -> str:
    return loopback("GET", base_url + "/api/config", headers=auth_headers(auth_token)).json()["llm_hub_url"]


def walk_hub_sheet(page: Page, base_url: str, auth_token: str) -> None:
    row = page.locator('#paneSettings [data-settings-value="hub"]')
    open_settings_sheet(page, "hubSheet")
    url, result = page.locator("#hubUrl"), page.locator("#hubResult")
    expect(url).to_have_value("")
    expect(row).to_have_text("Off")

    url.fill("http://example.com")
    url.press("Enter")
    expect(result).to_contain_text("private network")
    assert _saved(base_url, auth_token) == ""

    with _fake_hub() as hub:
        url.fill(hub)
        url.press("Enter")
        expect(result).to_contain_text("Saved. The hub answered")
        assert _saved(base_url, auth_token) == hub
        expect(row).to_have_text(hub.removeprefix("http://"))

    # A refusal that lands before the next frame finds the field still focused:
    # WebKit drops focus from a disabled field only when it renders. The
    # stub answers in microtasks alone, so no frame can come first. The value
    # is read as the field unlocks: the passkey status poll repaints an
    # unfocused field later and would hide a field left on the refused address.
    page.evaluate("""() => {
        const field = document.getElementById('hubUrl');
        new MutationObserver((_, observer) => {
            if (field.disabled) return;
            field.dataset.atUnlock = field.value;
            observer.disconnect();
        }).observe(field, { attributeFilter: ['disabled'] });
        const real = window.fetch;
        window.fetch = (url, opts) => {
            if (url !== '/api/config' || opts?.method !== 'POST') return real(url, opts);
            window.fetch = real;
            const body = { detail: 'Refused at once.' };
            return Promise.resolve({ ok: false, status: 400, json: () => Promise.resolve(body) });
        };
    }""")
    url.fill("http://127.0.0.1:2")
    url.press("Enter")
    expect(result).to_contain_text("Refused at once.")
    expect(url).to_have_attribute("data-at-unlock", hub)

    # The stand-in is gone: its port no longer answers, and the old value stays.
    url.fill(hub.replace("127.0.0.1", "localhost"))
    url.press("Enter")
    expect(result).to_contain_text("Nothing is listening")
    expect(url).to_have_value(hub)
    assert _saved(base_url, auth_token) == hub

    url.fill("")
    url.press("Enter")
    expect(result).to_contain_text("No hub is set up")
    assert _saved(base_url, auth_token) == ""
    close_settings_sheets(page)
    expect(row).to_have_text("Off")
