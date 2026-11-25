# Install, update and move App Launcher Lite

App Launcher Lite moves to another Windows PC by cloning it and running one `setup.bat`. An update is a pull plus a restart. Neither resets or overwrites what a PC has configured.

## What you need

- Windows 10 or 11, `git`, and Python 3.11 or newer (the `py` launcher, or `python` on `PATH`).
- The GitHub Copilot CLI and `glab`, each signed in: [installing-agent-clis.md](installing-agent-clis.md).
- Optional: [Tailscale](https://tailscale.com/) for the phone-trusted HTTPS certificate.

## Fresh PC

```powershell
git clone <this repository> app-launcher-lite
cd app-launcher-lite
.\setup.bat
.\tray.bat
```

`setup.bat` runs `src/installer.py`, which reads the `[install]` table of `.fleet.toml` and:

1. creates `.venv` when it is missing;
2. installs `requirements.txt`, and later reinstalls only when that file changed (its hash is kept inside `.venv`);
3. creates each ignored config (`config/config.json`, `config/webapp_config.json`, `config/apps.json`, `config/jobs.json`) from its tracked sample when it is missing;
4. ends with a checklist that marks each item `ok`, `missing` or `unknown`.

It then offers the Copilot hooks and skills ([copilot/docs/install.md](../copilot/docs/install.md)); answer `n` to skip them and install them later.

The checklist names what still needs you:

- **Sign-in password:** `.venv\Scripts\python.exe -m src.password set`.
- **HTTPS certificate:** `.venv\Scripts\python.exe scripts\gen_tailscale_cert.py` (see the README's HTTPS section).
- **Copilot CLI** and **GitLab CLI (glab):** [installing-agent-clis.md](installing-agent-clis.md).
- **Start at log on:** run `.\setup.bat -Startup`, or turn it on in Settings.

The whole path from a machine with nothing on it to a first lane, with a proof for each step: [fresh-machine-checklist.md](fresh-machine-checklist.md).

`.\setup.bat -WhatIf` lists every change it would make, makes none and asks nothing.

## Update

```powershell
git pull --ff-only
.\tray.bat --restart
```

Nothing else is needed. Every file the app writes while it runs (configs, state, logs, certificates, `.venv`) is gitignored or outside the folder, and no tracked file is rewritten at runtime, so `git status` stays clean and the pull fast-forwards. The ports come from this PC's `config/config.json`, so moving a port never means editing a tracked file.

`tray.bat --restart` installs no packages and keeps the session-host (your live sessions) running. When a pull changed `requirements.txt`, run `.\setup.bat` before the restart.

Running `setup.bat` on a configured PC is safe, and it is how a new setting arrives:

- Settings files (`config/config.json`, `config/webapp_config.json`): an existing value is never changed. A key the sample has and the file lacks is added, after a timestamped backup beside it (`<name>.json.bak-<YYYYmmdd-HHMMSS>`), and the added key names are printed. The backup holds the same secrets as the config and is gitignored. A file that does not parse is left alone and reported, and the run exits 1.
- Registries (`config/apps.json`, `config/jobs.json`) are your own lists: created when missing, never rewritten or reformatted.
- A second run reports `unchanged` and writes nothing.

## Migrate from an existing install

Use this on a PC that already runs an older copy of App Launcher Lite: one that may not be a git clone, and may live at another path. The old copy stays on disk until you say otherwise.

### 1. Clone fresh beside it

```powershell
git clone <this repository> <new folder>
```

Never clone over the old folder. A folder next to the old one keeps the defaults that look at sibling folders (the projects folder, `team-os`).

### 2. Copy this PC's own files across

Copy only these, each to the same relative path. All are gitignored, and any of them may be absent on a given PC. Do not copy `.venv`: `setup.bat` builds a new one.

| Kind | From the old folder |
|---|---|
| Settings and registries | `config/config.json`, `config/webapp_config.json`, `config/apps.json`, `config/jobs.json` |
| Sign-in | `config/app_password.json` (password hash), `config/webauthn_devices.json` (enrolled passkeys) |
| Certificates and tunnel | `webapp/certificates/`, `webapp/cloudflared.yml` (the tunnel's credentials stay in your user profile) |
| Your theme overrides | `webapp/terminal-themes.json` |
| History | `webapp/jobs/` (Job runs), `webapp/sessions/` (session logs and transcripts) |
| Chat and dictation files | `webapp/outbox/`, `webapp/briefs/`, `webapp/recordings/` |
| Chief and Board state | `webapp/chief-pointer.json`, `webapp/chief-answered.json`, `webapp/chief-journal-look.json`, `webapp/owner-check-marks.json`, `webapp/jobs-dismissed.json` |
| Other state | `webapp/spawned-apps.json`, `webapp/lid-keep-awake.json` (the power plan values the lid switch restores), `webapp/progress-totals.json` |

The other files under `webapp/` are logs, caches and reports; leave them. Two things live outside the folder and stay where they are: the chief's and the sessions' state in `~\.copilot\app-launcher-lite-state\`, and the Job index and usage log in the machine's runtime data folder (`C:\sqlite\app-launcher-lite\` by default).

Then open the copied configs and repoint any value that names a path **inside the old folder** (for example a Copilot or skills folder, or an app or Job script).

### 3. Top them up

```powershell
cd <new folder>
.\setup.bat -WhatIf
.\setup.bat
```

Settings files gain only the keys they lack; the values you copied stay as they were. Answer `n` to the Copilot question: step 5 does it, after the old copy's hooks are gone.

### 4. Stop the old copy

1. Finish or save your open sessions: this step ends them all.
2. Choose **Quit** in the old tray's menu. That stops its webapp, tunnel and session-host.
3. Confirm both ports are free (the numbers are `webapp.port` and `webapp.session_host_port` in `config/config.json`); this prints nothing when they are:

   ```powershell
   Get-NetTCPConnection -State Listen -LocalPort 8465,8466 -ErrorAction SilentlyContinue
   ```

   A copy still listening there would be adopted by the new tray.
4. Copy the history, chat, chief and other state rows of step 2 once more, so nothing written since the first copy is lost. Leave the configs: the new ones are already topped up.
5. Delete `AppLauncherLite.bat` from your Startup folder (`shell:startup`). It starts the old folder, and while it is there the checklist reads `unknown` for **Start at log on** and `-Startup` leaves it alone.
6. Remove the old copy's Copilot hooks and skills, run **from the old folder**: `.venv\Scripts\python.exe copilot\install.py --uninstall`. The new install keeps any hook that runs another folder's script, so skipping this leaves the old hooks in place. A copy that used fleet-config-lite follows [migrating-from-fleet-config-lite.md](migrating-from-fleet-config-lite.md) instead.

### 5. Start the new one

From the new folder:

```powershell
.\.venv\Scripts\python.exe copilot\install.py
.\tray.bat
.\setup.bat -Startup
```

Skip the last line if it should not start at log on.

Task Scheduler entries under `\AppLauncherLite\` still run the old folder. In the Jobs tab, open each scheduled job and save it: saving deletes and registers its entries again. A job set to run elevated or without a logged-on user is registered by hand from an elevated shell, so register it there again.

### 6. Verify it

- `curl.exe -sk https://127.0.0.1:8465/api/version` (`http://` without a certificate) shows a `git_sha` equal to `git rev-parse --short HEAD` and `session_host.reachable` `true`.
- Sign in from the phone, open a session, and run one Job.
- Every scheduled entry names the new folder:

  ```powershell
  Get-ScheduledTask -TaskPath '\AppLauncherLite\' | ForEach-Object { "$($_.TaskName): $($_.Actions.Arguments)" }
  ```

If anything is wrong, quit the new tray, delete its Startup entry, and run `copilot\install.py --uninstall` from the new folder. Then in the old folder run `copilot\install.py`, `tray.bat`, and save the scheduled jobs again.

### 7. Retire the old folder, only after you confirm

Nothing in this playbook or in `setup.bat` deletes the old copy. When you are satisfied, rename it first (for example `app-launcher-lite.retired`) and run on the new copy for a while; delete it yourself once you are sure. An agent working through this playbook asks you before it deletes anything.

## Execution policy

Nothing here needs a signed script or overrides the PC's PowerShell execution policy, `AllSigned` included. `setup.bat`, `tray.bat` and `scripts/webapp.bat` are batch files, and both installers are Python. `tray.bat` starts its helper through `src/launching/ps_script.py`, as does the operator-only session-host restart (`.venv\Scripts\python.exe -m src.launching.ps_script scripts\restart-session-host.ps1 -Confirm`); it falls back to a ScriptBlock launch under `AllSigned` or `Restricted`. PowerShell jobs in the Jobs tab start the same way.

## Uninstall

1. Turn off the lid keep-awake switch if you use it, so your power plan is restored.
2. Turn off **Start at log on** in Settings, or delete `AppLauncherLite.bat` from your Startup folder (`shell:startup`).
3. Remove your job schedules in the Jobs tab: they are Task Scheduler tasks under the `\AppLauncherLite\` folder.
4. Remove the Copilot hooks and skills: `.venv\Scripts\python.exe copilot\install.py --uninstall` ([copilot/docs/install.md](../copilot/docs/install.md)).
5. Quit the tray from its menu.
6. Delete the `app-launcher-lite` folder. Keep a copy of the files listed under "Migrate" first if you may want them back. `~\.copilot\app-launcher-lite-state\` and the runtime data folder stay; delete them yourself if you want nothing left.
