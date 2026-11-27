# Install, update and migrate App Launcher Lite

App Launcher Lite installs, updates and migrates with one `setup.bat`: clone it and run it. An update is a pull plus a restart. A migration, onto a fresh clone on the same PC, is one `setup.bat` call. None of them resets or overwrites what a PC has configured.

## Install

You need Windows 10 or 11, `git`, and Python 3.11 or newer (the `py` launcher, or `python` on `PATH`), plus the GitHub Copilot CLI and `glab`, each signed in: [installing-agent-clis.md](installing-agent-clis.md). [Tailscale](https://tailscale.com/) is optional, for the phone-trusted HTTPS certificate.

```powershell
git clone <this repository> app-launcher-lite
cd app-launcher-lite
.\setup.bat
.\tray.bat
```

`setup.bat` runs `src/installer.py`, which reads the `[install]` tables of `.fleet.toml` and:

1. creates `.venv` when it is missing;
2. installs `requirements.txt`, and later reinstalls only when that file changed (its hash is kept inside `.venv`);
3. creates each ignored config (`config/config.json`, `config/webapp_config.json`, `config/apps.json`, `config/jobs.json`) from its tracked sample when it is missing;
4. ends with two lists: the required items, each `ok`, `missing` or `unknown`, and the optional features, each `available`, `off` or `unknown` with what it unlocks.

It then offers the Copilot hooks and skills ([copilot/docs/install.md](../copilot/docs/install.md)); answer `n` to skip them and install them later.

The optional features, each needed only for what it unlocks:

- **Sign-in password** (remote sign-in): `.venv\Scripts\python.exe -m src.password set`.
- **HTTPS certificate** (phone access): `.venv\Scripts\python.exe scripts\gen_tailscale_cert.py` (see the README's HTTPS section).
- **Copilot CLI** and **GitLab CLI (glab)** (Copilot sessions, the Board's GitLab data): [installing-agent-clis.md](installing-agent-clis.md).
- **Start at log on:** run `.\setup.bat -Startup`, or turn it on in Settings.

The whole path from a machine with nothing on it to a first lane, with a proof for each step: [fresh-machine-checklist.md](fresh-machine-checklist.md). `.\setup.bat -WhatIf` lists every change it would make, makes none and asks nothing.

### Execution policy

Nothing here needs a signed script or overrides the PC's PowerShell execution policy, `AllSigned` included. `setup.bat`, `tray.bat` and `scripts/webapp.bat` are batch files, and both installers are Python. `tray.bat` starts its helper through `src/launching/ps_script.py`, as does the operator-only session-host restart (`.venv\Scripts\python.exe -m src.launching.ps_script scripts\restart-session-host.ps1 -Confirm`); it falls back to a ScriptBlock launch under `AllSigned` or `Restricted`. PowerShell jobs in the Jobs tab start the same way.

### Uninstall

1. Turn off the lid keep-awake switch if you use it, so your power plan is restored.
2. Turn off **Start at log on** in Settings, or delete `AppLauncherLite.bat` from your Startup folder (`shell:startup`).
3. Remove your job schedules in the Jobs tab: they are Task Scheduler tasks under the `\AppLauncherLite\` folder.
4. Remove the Copilot hooks and skills: `.venv\Scripts\python.exe copilot\install.py --uninstall` ([copilot/docs/install.md](../copilot/docs/install.md)).
5. Quit the tray from its menu.
6. Delete the `app-launcher-lite` folder. Keep a copy of the files listed under [Migrate](#migrate) first if you may want them back. `~\.copilot\app-launcher-lite-state\` and the runtime data folder stay; delete them yourself if you want nothing left.

## Update

```powershell
git pull --ff-only
.\setup.bat
.\tray.bat --restart
```

Every file the app writes while it runs (configs, state, logs, certificates, `.venv`) is gitignored or outside the folder, and no tracked file is rewritten at runtime, so `git status` stays clean and the pull fast-forwards. The ports come from this PC's `config/config.json`, so moving a port never means editing a tracked file. `tray.bat --restart` installs no packages and keeps the session-host (your live sessions) running.

`setup.bat` on a configured PC is safe, and it is how a new setting or a new requirement arrives:

- Settings files (`config/config.json`, `config/webapp_config.json`): an existing value is never changed. A key the sample has and the file lacks is added, after a timestamped backup beside it (`<name>.json.bak-<YYYYmmdd-HHMMSS>`), and the added key names are printed. The backup holds the same secrets as the config and is gitignored. A file that does not parse is left alone and reported, and the run exits 1.
- Registries (`config/apps.json`, `config/jobs.json`) are your own lists: created when missing, never rewritten or reformatted.
- A second run reports `unchanged` and writes nothing.

## Migrate

Moves this PC's install onto a fresh clone. What moves is declared in `[install.migrate]` of `.fleet.toml`:

| Kind | Copied |
|---|---|
| Settings and registries | `config/config.json`, `config/webapp_config.json`, `config/apps.json`, `config/jobs.json` |
| Sign-in | `config/app_password.json` (password hash), `config/webauthn_devices.json` (enrolled passkeys) |
| Certificates, tunnel, themes | `webapp/certificates/`, `webapp/cloudflared.yml`, `webapp/terminal-themes.json` |
| History | `webapp/jobs/` (Job runs), `webapp/sessions/` (session logs and transcripts) |
| Chat and dictation files | `webapp/outbox/`, `webapp/briefs/`, `webapp/recordings/` |
| Chief, Board and other state | `webapp/chief-pointer.json`, `webapp/chief-answered.json`, `webapp/chief-journal-look.json`, `webapp/owner-check-marks.json`, `webapp/jobs-dismissed.json`, `webapp/spawned-apps.json`, `webapp/lid-keep-awake.json`, `webapp/progress-totals.json` |

Logs, caches and reports are not copied. Two things live outside the folder and stay where they are, shared by both copies: the chief's and the sessions' state in `~\.copilot\app-launcher-lite-state\`, and the Job index and usage log in the runtime data folder `app-launcher-lite` (`C:\sqlite\app-launcher-lite\` by default). A file that is already in the new clone is never overwritten, secrets are compared by hash and never printed, and nothing here or in `setup.bat` deletes the old copy.

### Before you start

Migration stops App Launcher Lite and every session it hosts. Run it from a plain terminal, never from a session the app hosts: the stop refuses when it would end its own run. From outside a session:

1. Finish or save your open sessions, and close any terminal or editor whose folder is inside the app.
2. Finish or remove the app folder's git worktrees (`git worktree list`): they stay tied to the old folder's `.git`. Uncommitted changes and local branches stay in the old copy too.
3. Choose **Quit** in the tray's menu. The installer finds a running copy by the folder path in its processes' command lines, so a copy started under another spelling of that path (a junction, a mapped drive) is only partly found: the tray is missed, and it restarts what the stop ended. Quit stops the whole copy however it was started.
4. If Tailscale Serve publishes the webapp's port, turn that mapping off. `tailscale serve status` lists it with its target; note both. `tailscaled` listens on the same port number, and the installer counts it as the old copy still holding the port: the run fails after it has already stopped the app, with nothing renamed.

   ```powershell
   tailscale serve status
   tailscale serve --https=<port> off
   ```

   After the migration, add it back with the target you noted: `tailscale serve --bg --https=<port> <target>`.

### Reinstall in the same folder (the usual case)

From the folder above the app:

```powershell
cd <parent folder>
.\app-launcher-lite\setup.bat -InPlace -WhatIf    # the backup name, what it would stop, every file it would copy
.\app-launcher-lite\setup.bat -InPlace
```

`-WhatIf` changes nothing. The real run stops what still runs from the folder, renames it to `app-launcher-lite.bak-YYYYMMDD-N`, clones the remote's default branch into the original path, copies the files above, runs the install, starts the tray, and passes only when the new clone answers `/api/version` with its own commit from a process in that folder. The path does not change, so the Startup link, the Copilot hooks and the scheduled Jobs need no edit.

### Beside it (another folder, or an old copy that is not a git clone)

```powershell
git clone <this repository> <new folder>
cd <new folder>
.\setup.bat -MigrateFrom <old folder> -StopOld
```

`-StopOld` is needed on one PC, where both copies would use the same ports. The run ends with what still runs from or points at the old folder. A path inside the old folder in a copied JSON config is rewritten to the new one.

### Only you can do these steps

- **After a move to another folder, repoint what stored the old path.** Run `.venv\Scripts\python.exe copilot\install.py --uninstall` in the old folder, then `.venv\Scripts\python.exe copilot\install.py` in the new one (the new install keeps a hook that runs another folder's script). Delete `AppLauncherLite.bat` from your Startup folder and run `.\setup.bat -Startup` in the new one. In the Jobs tab, open each scheduled job and save it, which registers its Task Scheduler entry again; register one set to run elevated or without a logged-on user from an elevated shell. A copy that used fleet-config-lite follows [migrating-from-fleet-config-lite.md](migrating-from-fleet-config-lite.md) instead of the first of these.
- **Check it from the phone:** sign in, open a session and run one Job.
- **Fall back** if verify fails (the report names the step): stop the new copy, rename the new folder away and the backup to its original name, and start it; it is untouched.
- **Retire the backup** once you are sure the new copy works, by deleting it yourself. An agent working through this playbook asks you before it deletes anything.
