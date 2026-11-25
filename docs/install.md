# Install and update

App Launcher Lite moves to another Windows PC the way you copy an app: clone it and run one install. On a PC that already runs it, an update is `git pull` plus a restart, and nothing else. An install or an update never resets or overwrites what that PC has configured.

## What you need

- Windows 10 or 11 and Python 3 (the `py` launcher or `python` on `PATH`).
- `git`.
- Optional: [Tailscale](https://tailscale.com/) for the phone-trusted HTTPS certificate.

## Fresh PC

```powershell
git clone <this repository>
cd app-launcher-lite
.\setup.bat
.\tray.bat
```

`setup.bat` runs `scripts/install.py` (the work is `src/launching/installer.py`), which:

1. creates `.venv` when it is missing;
2. installs `requirements.txt`, and later reinstalls only when that file changed (its hash is recorded inside `.venv`);
3. creates each ignored `config/*.json` from its tracked `config/*.sample.json` when the file is missing;
4. ends with a checklist that marks each item `ok`, `missing` or `unknown`: the venv, the requirements, each config, the sign-in password, the HTTPS certificate and the log-on autostart.

It then offers the Copilot hooks and skills ([copilot/docs/install.md](../copilot/docs/install.md)); answer `n` to skip them and install them later.

The checklist names what still needs you. The usual first-run items:

- **Sign-in password:** `.venv\Scripts\python.exe -m src.password set`.
- **HTTPS certificate:** `.venv\Scripts\python.exe scripts\gen_tailscale_cert.py` (see the README's HTTPS section).
- **Start at log on:** run `.\setup.bat -Startup`, or turn it on in Settings.

The coding agent's CLI and `glab` are installed separately: [installing-agent-clis.md](installing-agent-clis.md).

## Already-configured PC

Update with:

```powershell
git pull --ff-only
.\tray.bat --restart
```

No other step is needed. Every file the app writes while it runs (configs, state, logs, `tmp/`, certificates, `.venv`) is gitignored, and no tracked file is rewritten at runtime, so `git status` stays clean and the pull always fast-forwards. The ports in `tray.bat`, `scripts/webapp.bat` and `scripts\restart-session-host.ps1` come from this PC's ignored config through `scripts/webapp_port.py`, so moving a port never means editing a tracked file.

Running `setup.bat` on a configured PC is safe and is how you pick up a new setting:

- Settings files (`config/config.json`, `config/webapp_config.json`): an existing value is never changed. A key the sample has and the file lacks is added, after a timestamped backup beside it (`config/<name>.json.bak-<YYYYmmdd-HHMMSS>`), and the added key names are printed. A file that does not parse is left alone and reported, and the run exits 1.
- Registries (`config/apps.json`, `config/jobs.json`) are your own lists: created when missing, never rewritten or reformatted.
- The log-on autostart entry is added only with `-Startup` and only when absent. An existing one is reported `kept`.
- A second run reports `unchanged` and writes nothing.

`.\setup.bat -WhatIf` lists every change it would make and makes none, and asks nothing.

## Execution policy

Nothing here needs a signed script or overrides the PC's PowerShell execution policy, `AllSigned` included. `setup.bat`, `tray.bat` and `scripts/webapp.bat` are batch files, and the Copilot install (`copilot\install.py`) is Python. `tray.bat` starts its helper through `src/launching/ps_script.py`, as does the operator-only session-host restart (`.venv\Scripts\python.exe -m src.launching.ps_script scripts\restart-session-host.ps1 -Confirm`); it falls back to a ScriptBlock launch under `AllSigned` or `Restricted`. PowerShell jobs in the Jobs tab start the same way.

## Uninstall

1. Turn off **Start at log on** in Settings, or delete `AppLauncherLite.bat` from your Startup folder (`shell:startup`).
2. Remove any job schedules you made in the Jobs tab: they are Task Scheduler tasks under the `\AppLauncherLite\` folder.
3. Remove the Copilot hooks and skills with `.venv\Scripts\python.exe copilot\install.py --uninstall` ([copilot/docs/install.md](../copilot/docs/install.md)).
4. Quit the tray from its menu, then delete the `app-launcher-lite` folder.
