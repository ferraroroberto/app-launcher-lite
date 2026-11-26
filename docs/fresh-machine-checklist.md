# Fresh-machine checklist

The whole path from a Windows machine with nothing on it to a lane that runs end to end, in order. Every step ends with a **Proof** command and what it must print. A step passes only when its proof does; one that was not run is `unknown`, never assumed. Run the commands in PowerShell, from the checkout's root once it exists.

The reasons behind each step live in the docs it links: [installing-agent-clis.md](installing-agent-clis.md), [../copilot/docs/install.md](../copilot/docs/install.md) (what each installer line means), [gitlab.md](gitlab.md), [config.md](config.md).

## 1. Python and Git

Python 3.12 or newer and Git for Windows, both on `PATH`:

```powershell
winget install -e --id Python.Python.3.12
winget install -e --id Git.Git
```

Open a new terminal afterwards so `PATH` is current.

**Proof:** `python --version` prints `Python 3.12` or newer, and `git --version` prints a version.

## 2. Copilot CLI

```powershell
winget install -e --id GitHub.Copilot
copilot
```

Run `/login` inside the session and finish the sign-in (it needs an active GitHub Copilot subscription), then leave it.

**Proof:** `copilot --version` prints a version. Capability facts are recorded per version, so run `python copilot\scripts\copilot_probe.py run` after the clone (step 4): it spends nothing and prints one `PROBE` line per item with `verified`, `unsupported` or `unknown`. An item the installed version has not been probed on reads `unknown`; that is the honest answer, not a failure. Facts recorded in the README are for the version it names.

## 3. glab and the GitLab sign-in

```powershell
winget install -e --id GLab.GLab
glab auth login --hostname <your GitLab host>
```

**Proof:** `glab auth status --hostname <your GitLab host>` exits 0 and prints `Logged in to`.

Git has its own credential helper, so being signed in to `glab` does not let `git fetch` or `git push` through. A lane runs with prompts off and stops on its first fetch without it. The check is step 6; if it reports `missing`, it prints the fix to run yourself.

## 4. Clone and set up

```powershell
git clone <this repo's URL> app-launcher-lite
cd app-launcher-lite
.\setup.bat
```

`setup.bat` creates `.venv`, installs `requirements.txt`, creates the configs and asks whether to run the Copilot install ([install.md](install.md) lists what it does). Answer `n` here; step 6 runs it with its checklist.

**Proof:** `.\.venv\Scripts\python.exe -c "import fastapi, uvicorn, psutil, PIL; print('deps ok')"` prints `deps ok`.

## 5. Your values in the configs

Step 4's `setup.bat` created each real config from its sample. All four are gitignored, and a rerun never overwrites a value. Set, in `config\webapp_config.json` or under Settings once the tray is up:

- `projects_dir`: the folder whose child directories the Coding tab lists.
- `gitlab_group`, and `gitlab_host` for a self-hosted instance.
- `copilot_models`: trim it to the ids the account offers; set `copilot_model` to `""` (auto) when none is available.

**Proof:** `.\.venv\Scripts\python.exe -c "import json; [json.load(open(f'config/{n}.json')) for n in ('config','webapp_config','apps','jobs')]; print('config parses')"` prints `config parses`. For the GitLab reads, `glab api "groups/<group>/issues?state=opened&per_page=1"` answers a JSON array (an empty `[]` is a valid answer for a group with no open issues).

## 6. Copilot hooks and skills

Preview, install, and read the checklist the installer prints:

```powershell
.\.venv\Scripts\python.exe copilot\install.py --what-if
.\.venv\Scripts\python.exe copilot\install.py
```

**Proof:** the second run prints two `[ok] hook config` lines, one `[ok] skill junction` line per skill (a machine that shares its skills folder prints `[skip]` lines instead), and a "Fresh-machine checklist" in which each of these reads `[ok]`: `Copilot CLI`, `glab signed in`, `git reaches GitLab without a prompt`, `venv`. A `[missing]` line says what to fix; fix it and run again, and a third run reads `[ok] unchanged` throughout. A `[unknown]` line is not a pass: `git access ... not confirmed` means no https GitLab remote was found to ask (run it from a checkout whose `origin` or `gitlab` remote is https), and `session-host not reachable` is expected until step 7.

Restart any open Copilot session afterwards: hook configs load at CLI startup only.

**Proof of the hooks:** `copilot -p "hi" --allow-all-tools`, then `Test-Path $HOME\.copilot\app-launcher-lite-state\sessions-state.json` prints `True`.

## 7. Start the tray

```powershell
.\tray.bat
```

**Proof:** `curl.exe -s http://127.0.0.1:8465/api/version` (the port is `webapp.port` in `config\config.json`) prints JSON whose `git_sha` equals `git rev-parse --short HEAD` and whose `session_host.reachable` is `true`. Then open `http://127.0.0.1:8465` and see the five tabs.

## 8. The chief

On the Board, send a message in the chief's chat (or tap Start). Settings > Chief > **Skills folder** stays empty to use the bundled issue skills.

**Proof:** the line under Skills folder reads `found (bundled)` for the four issue skills, the chief's card appears and it answers.

## 9. One lane, end to end

Tap **▶ Start** on a small Backlog issue of a project under `projects_dir` (or ask the chief to dispatch it).

**Proof:** the lane's session opens, cuts a branch in a worktree, fetches and pushes with no password prompt, and reaches a merge request. The merge step (`glab mr merge --auto-merge`) was run live on a GitLab test project, not on a machine set up from this checklist; see [gitlab.md](gitlab.md#verified-live-and-not).

## What has been proven

| Steps | State |
| --- | --- |
| The proof commands of steps 2, 4 (the venv's Python), 6 and 7, on the build machine, which already carries the setup | run and passed there, on Copilot CLI 1.0.114, `glab` 1.131 and Python 3.14: `copilot --version`, `glab auth status`, the probe's static run, the installer's `-WhatIf` and the git-access check (`ok`), and `/api/version` with the session-host reachable |
| Steps 1, 5, 8 and 9, on the build machine | `unknown`: not run there (the steps that install tools or start a lane on a machine that has none) |
| The checklist on a clean machine | followed and validated by the owner; no per-step proof output is recorded, so a single step's result there is `unknown` |

Record each break found on a clean machine by fixing the step above, so the list stays the one that works.
