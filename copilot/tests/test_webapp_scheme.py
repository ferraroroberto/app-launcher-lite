"""Chief verbs reach the webapp over whichever scheme Lite serves.

Each serve mode is a local fake server on loopback, plain or TLS, answering the
same routes; nothing here touches the live Lite or its session-host.
"""

from __future__ import annotations

import datetime
import ipaddress
import json
import socket
import ssl
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from test_chief_lanes import StubbedLauncherCase, StubLauncher, _card, _ops

from chief import launcher

# The resolver under test; `skills/_lib` is on sys.path only once test_chief_lanes is imported.
lite_webapp = launcher.lite_webapp

# What the webapp port serves in each `serve_over` mode (app/webapp/serve_modes.py's
# `serve_plan`): `both` puts HTTPS on the webapp port and plain HTTP on a second port.
MODES = {
    "http": "http",
    "https": "https",
    "both": "https",
    "auto with a cert": "https",
    "auto without a cert": "http",
}

_BOARD = {"columns": {"claude_turn": [_card("lane-1", "demo")], "your_turn": []}, "running_jobs": []}


def _self_signed(folder: Path) -> ssl.SSLContext:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                       critical=False)
        .sign(key, hashes.SHA256())
    )
    (folder / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (folder / "key.pem").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(folder / "cert.pem", folder / "key.pem")
    return ctx


class _TlsServer(ThreadingHTTPServer):
    """A plain `ThreadingHTTPServer` whose accepted sockets speak TLS, handshaking in the
    handler thread; a plaintext request then fails there and the connection drops."""

    tls: ssl.SSLContext

    def get_request(self):
        sock, addr = self.socket.accept()
        return self.tls.wrap_socket(sock, server_side=True, do_handshake_on_connect=False), addr

    def handle_error(self, request, client_address) -> None:
        pass  # a plaintext caller failing the handshake is the case under test


def _serve(stub: StubLauncher, scheme: str, tls: ssl.SSLContext, port: int = 0) -> None:
    """Re-bind `stub`'s handler on `port` (0 = any) over `scheme`, replacing its server."""
    handler = stub.server.RequestHandlerClass
    stub.close()
    if scheme == "https":
        server = _TlsServer(("127.0.0.1", port), handler)
        server.tls = tls
    else:
        server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    stub.server = server
    stub.url = f"http://127.0.0.1:{server.server_address[1]}"
    threading.Thread(target=server.serve_forever, daemon=True).start()


class SchemeCase(StubbedLauncherCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._certs = tempfile.TemporaryDirectory()
        cls.tls = _self_signed(Path(cls._certs.name))

    @classmethod
    def tearDownClass(cls) -> None:
        cls._certs.cleanup()

    def setUp(self) -> None:
        super().setUp()
        lite_webapp._schemes.clear()
        self.addCleanup(lite_webapp._schemes.clear)
        self.stub.routes.update({
            "/api/board": (200, _BOARD),
            "/api/claude-code/roster": (200, {"sessions": []}),
        })


class EveryServeModeTests(SchemeCase):
    def test_board_and_sessions_read_in_every_mode(self) -> None:
        for mode, scheme in MODES.items():
            with self.subTest(mode=mode):
                lite_webapp._schemes.clear()
                _serve(self.stub, scheme, self.tls)
                # The chief's default base URL is always plain http://127.0.0.1:<webapp port>.
                code, out = _ops("board", "--json", "--base-url", self.stub.url)
                self.assertEqual(code, 0, out)
                self.assertEqual(json.loads(out)["columns"]["claude_turn"][0]["session_id"], "lane-1")
                code, out = _ops("sessions", "--base-url", self.stub.url)
                self.assertEqual(code, 0, out)
                self.assertIn("demo", out)
                self.assertNotIn("SOURCE=session-host", out)

    def test_probe_names_each_listener(self) -> None:
        for scheme in ("http", "https"):
            with self.subTest(scheme=scheme):
                _serve(self.stub, scheme, self.tls)
                self.assertEqual(lite_webapp.probe_scheme("127.0.0.1", self.stub.server.server_address[1]), scheme)

    def test_nothing_listening_is_unreachable_not_wrong_scheme(self) -> None:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        code, out = _ops("board", "--base-url", f"http://127.0.0.1:{port}")
        self.assertEqual(code, 2, out)
        self.assertIn("UNREACHABLE=", out)


class WrongSchemeTests(SchemeCase):
    def setUp(self) -> None:
        super().setUp()
        _serve(self.stub, "https", self.tls)
        self.port = self.stub.server.server_address[1]
        # A stale answer from before Lite restarted on HTTPS.
        lite_webapp._schemes[("127.0.0.1", self.port)] = "http"

    def test_a_read_over_plain_http_falls_back_to_https(self) -> None:
        board = launcher._request(self.stub.url, "/api/board")
        self.assertEqual(board["columns"]["claude_turn"][0]["session_id"], "lane-1")
        self.assertEqual(lite_webapp._schemes[("127.0.0.1", self.port)], "https")

    def test_a_write_dropped_over_plain_http_is_wrong_scheme_and_not_resent(self) -> None:
        self.stub.routes["/api/x"] = (200, {"ok": True})
        with self.assertRaises(lite_webapp.WrongSchemeError) as caught:
            launcher._request(self.stub.url, "/api/x", method="POST", body={"a": 1})
        self.assertEqual(self.stub.posts, [], "a dropped write is never resent")
        line, code = launcher.transport_failure(caught.exception)
        self.assertTrue(line.startswith("WRONG_SCHEME="), line)
        self.assertEqual(code, 2)
        # The next call already uses the scheme the port serves.
        self.assertEqual(launcher._request(self.stub.url, "/api/x", method="POST", body={"a": 2}), {"ok": True})
        self.assertEqual(self.stub.posts, [("/api/x", {"a": 2})])

    def test_a_write_over_a_failed_tls_handshake_is_sent_once_over_http(self) -> None:
        _serve(self.stub, "http", self.tls, port=self.port)
        lite_webapp._schemes[("127.0.0.1", self.port)] = "https"
        self.stub.routes["/api/x"] = (200, {"ok": True})
        self.assertEqual(launcher._request(self.stub.url, "/api/x", method="POST", body={"a": 1}), {"ok": True})
        self.assertEqual(self.stub.posts, [("/api/x", {"a": 1})])


class ServeOverChangeTests(SchemeCase):
    def test_the_next_call_after_a_switch_finds_the_new_scheme(self) -> None:
        _serve(self.stub, "http", self.tls)
        port = self.stub.server.server_address[1]
        self.assertEqual(_ops("board", "--json", "--base-url", self.stub.url)[0], 0)
        for scheme in ("https", "http"):
            with self.subTest(now=scheme):
                _serve(self.stub, scheme, self.tls, port=port)
                code, out = _ops("board", "--json", "--base-url", self.stub.url)
                self.assertEqual(code, 0, out)
                self.assertEqual(lite_webapp._schemes[("127.0.0.1", port)], scheme)


class SessionHostTests(unittest.TestCase):
    def test_the_session_host_port_is_never_probed_or_rewritten(self) -> None:
        url = "http://127.0.0.1:18466"
        with mock.patch.object(lite_webapp.lite_ports, "session_host_port", return_value=18466), \
                mock.patch.object(lite_webapp, "probe_scheme") as probe:
            self.assertEqual(lite_webapp.resolve(url), url)
        probe.assert_not_called()

    def test_a_non_loopback_host_is_refused_before_any_probe(self) -> None:
        with mock.patch.object(lite_webapp, "probe_scheme") as probe:
            with self.assertRaises(ValueError):
                launcher._request("http://192.0.2.1:8465", "/api/board")
        probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
