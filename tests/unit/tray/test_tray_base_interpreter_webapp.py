"""The restart recognises its own web app on the base interpreter, and checks it over HTTPS.

A ``.venv`` launcher that re-executes the shared Python leaves a web app whose command line names no
``.venv``. What still names the checkout is its TLS arguments: ``webapp\\certificates\\cert.pem`` and
``key.pem``. The build check gets one explicit HTTPS URL when the webapp serves HTTPS, so a failure
names the HTTPS reason instead of the plain-HTTP attempt that followed it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app.webapp import manager
from app.webapp.serve_modes import ServePlan
from scripts import webapp_port
from src.subprocess_flags import NO_WINDOW
from tests.unit.tray.test_tray_lifecycle_reclaim import REPO_ROOT, _owns

REPO = "C:" + "\\stubfolder\\app-launcher-lite"
BASE = '"C:\\Python314\\pythonw.exe" -m app.webapp.serve --host 0.0.0.0 --port 8475 '


def _tls(root: str) -> str:
    certs = root + "\\webapp\\certificates"
    return f"--ssl-keyfile {certs}\\key.pem --ssl-certfile {certs}\\cert.pem"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell")
@pytest.mark.windows_only
class TestBaseInterpreterWebapp:
    def test_a_webapp_naming_this_checkouts_certificates_is_owned(self, tmp_path: Path) -> None:
        worktree = "C:/stubfolder/app-launcher-lite-wt-478"
        owned = _owns(tmp_path, REPO, [
            BASE + _tls(REPO),
            # the other drive spelling of the junctioned root, quoted, in the `both` serve mode
            'python.exe -m app.webapp.serve_modes --http-port 8476 --ssl-certfile '
            '"E:/stubfolder/app-launcher-lite/webapp/certificates/cert.pem"',
            BASE + _tls("C:\\stubfolder\\app-launcher-lite-wt-478"),
        ], [worktree])
        assert owned == [True, True, True]

    def test_another_checkout_or_another_process_is_not_owned(self, tmp_path: Path) -> None:
        owned = _owns(tmp_path, REPO, [
            BASE + _tls("C:\\stubfolder\\app-launcher"),
            BASE + _tls("C:\\stubfolder\\app-launcher-lite-wt-999"),
            BASE + _tls("C:\\other\\stubfolder\\app-launcher-lite"),
            # no certificate to name the checkout: plain HTTP stays unclaimed
            BASE.strip(),
            # names the certificate without being the web app
            "notepad.exe " + REPO + "\\webapp\\certificates\\cert.pem",
        ])
        assert owned == [False] * 5


class TestVersionUrl:
    @pytest.mark.parametrize("certs, printed", [
        ((Path("cert.pem"), Path("key.pem")), "https://127.0.0.1:8475/api/version"),
        (None, ""),
    ])
    def test_the_resolver_prints_a_url_only_when_the_webapp_serves_https(
        self, monkeypatch, capsys, certs, printed
    ) -> None:
        monkeypatch.setattr(manager, "current_serve_plan", lambda: ServePlan(certs=certs))
        monkeypatch.setattr(webapp_port, "resolve_webapp_port", lambda: 8475)
        assert webapp_port.main(["--version-url"]) == 0
        assert capsys.readouterr().out.strip() == printed

    @pytest.mark.skipif(sys.platform != "win32", reason="tray.bat is Windows-only")
    @pytest.mark.windows_only
    def test_tray_bat_passes_that_url_to_the_build_check(self, tmp_path: Path) -> None:
        venv = REPO_ROOT / ".venv"
        if not (venv / "Scripts" / "python.exe").exists():
            pytest.skip("no .venv to borrow the interpreter from")
        repo = tmp_path / "repo"
        (repo / "scripts").mkdir(parents=True)
        (repo / ".venv" / "Scripts").mkdir(parents=True)
        shutil.copyfile(venv / "pyvenv.cfg", repo / ".venv" / "pyvenv.cfg")
        shutil.copyfile(venv / "Scripts" / "python.exe", repo / ".venv" / "Scripts" / "python.exe")
        shutil.copyfile(REPO_ROOT / "tray.bat", repo / "tray.bat")
        # Stand-in resolver and launcher: nothing real is read, detected or started.
        (repo / "scripts" / "__init__.py").write_text("", encoding="ascii")
        (repo / "scripts" / "webapp_port.py").write_text(
            "import sys\n"
            "print('https://127.0.0.1:8475/api/version' if '--version-url' in sys.argv else 8475)\n",
            encoding="ascii",
        )
        (repo / "src" / "launching").mkdir(parents=True)
        (repo / "src" / "launching" / "ps_script.py").write_text(
            "import os, sys\n"
            "args = sys.argv[1:]\n"
            "open(os.environ['TRAY_STUB_OUT'], 'w').write(args[args.index('-VersionUrl') + 1])\n",
            encoding="ascii",
        )
        (repo / "scripts" / "tray_lifecycle.ps1").write_text("# stand-in\n", encoding="ascii")
        out = tmp_path / "url.txt"
        env = {**os.environ, "TRAY_STUB_OUT": str(out)}
        env.pop("PYTHONPATH", None)
        done = subprocess.run(
            ["cmd.exe", "/d", "/c", str(repo / "tray.bat"), "--restart"],
            env=env, capture_output=True, timeout=120, creationflags=NO_WINDOW,
        )
        assert done.returncode == 0, done.stdout + done.stderr
        assert out.read_text() == "https://127.0.0.1:8475/api/version"
