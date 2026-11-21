# Installing the Copilot side

`copilot/install.ps1` connects this checkout to GitHub Copilot CLI for the current Windows user. It writes the two hook configs (session state for the Board, and the chief's guard), links the skills, adds any missing Lite config key, and prints a checklist. It never edits `~/.claude/settings.json`, an agent's trust settings or git config, and it never reads, moves or deletes `~/.copilot/hooks/state/`, where the chief's handover, plan, journal and mailbox live.

There are four cases below. Pick the one that matches the machine. Run every command from the checkout's root in PowerShell. The `ps_script` form works under every execution policy, `AllSigned` included.

## What the installer prints

Every line starts with one tag:

| Tag | Meaning |
|---|---|
| `[ok]` | Done, or already right. `[ok] unchanged -> <path>` means a re-run found nothing to change. |
| `[ok] kept (not Lite's) -> <path>` | Something that belongs to you or another tool sits there. It is left exactly as it is. When it is one of the four issue skills, the line adds `and it counts as <skill>`. |
| `[whatif]` | Only with `-WhatIf`: a change it would make. Nothing is written. |
| `[skip]` | Deliberately not done, with the reason (for example, a skills folder that is a link into another repo). |
| `[missing]` | Something to install or fix yourself; the line says what. |
| `[unknown]` | The installer could not find out. It is never treated as fine. |

## Where the issue skills come from

A lane runs `/issue-start` or `/issue-yolo` and ships with `/issue-finish`. Those skills, plus `/issue-add`, come from exactly one place:

- **`skills_dir` empty** (the default): Lite's own copies in `copilot/skills/`.
- **`skills_dir` set** to a folder holding `issue-add/`, `issue-start/`, `issue-yolo/` and `issue-finish/` (each with a `SKILL.md`): that folder. The installer links those four into `~/.copilot/skills/`, and into `~/.claude/skills/` when that is a real folder, and does not install Lite's bundled copies of them. Lite's other skills (`e2e`, `quick`, `_lib`) install as usual.

Settings > Chief > **Skills folder** holds the setting. The line under it reports `found`, `missing` or `unknown` for each skill and says where it resolved. A lane start, from the Board or from the chief, is refused while any of the four is missing or unknown, and the refusal names the skill.

## 1. A fresh machine

1. **Install the tools.** Install Python 3, Git, the Copilot CLI and `glab`, and sign in to both (steps: [installing-agent-clis.md](../../docs/installing-agent-clis.md)).
2. **Clone and set up.**

   ```powershell
   git clone <this repo's URL> app-launcher-lite
   cd app-launcher-lite
   .\setup.bat
   ```

   `setup.bat` creates `.venv`, installs the requirements and asks whether to run the Copilot install. Answer `y`, or run it yourself:

   ```powershell
   .\.venv\Scripts\python.exe -m src.launching.ps_script copilot\install.ps1
   ```

   **Check:** two `[ok] hook config -> ...app-launcher-lite-*.json` lines, one `[ok] skill junction -> ...` per skill, and a checklist where Copilot CLI, glab, venv and git access read `[ok]`. Fix each `[missing]` line as it says, then run the install again. A second run reports `[ok] unchanged` for everything it already did.
3. **Start Lite:** `.\tray.bat`, then open the webapp on the PC (its port is `port` in `config/webapp_config.json`, 8465 by default).
4. **Settings.** Under Settings > GitLab set the **GitLab group** and, for a self-hosted instance, the **GitLab host**. Under Settings > Folders set the **Projects folder**. Leave Settings > Chief > **Skills folder** empty to use Lite's bundled issue skills.

   **Check:** the line under Skills folder reads `found (bundled)` for all four skills.
5. **Start the chief** from the Board (send a message in the chief's chat, or tap Start).

   **Check:** the chief's card appears and it answers. Lite logs one line per issue skill when the chief starts.
6. **Run one test lane** end to end: tap **▶ Start** on a small Backlog issue, or ask the chief to dispatch one. A lane builds in a worktree and must fetch and push without a password prompt, which is why the checklist's git access line has to be `[ok]`.

   **Check:** the lane's session opens, cuts a branch and reaches the plan or the merge request.

## 2. A machine moving off fleet-config-lite

The full order, including what moves and what does not: [migrating-from-fleet-config-lite.md](../../docs/migrating-from-fleet-config-lite.md). In short:

1. Stop the chief at a turn end (a **Your turn** card, not mid-turn).
2. `git pull --ff-only` in this checkout, and leave the tray running.
3. Preview, then migrate:

   ```powershell
   .\.venv\Scripts\python.exe -m src.launching.ps_script copilot\install.ps1 -Migrate -WhatIf
   .\.venv\Scripts\python.exe -m src.launching.ps_script copilot\install.ps1 -Migrate
   ```

   It lists the old fleet-config-lite hook configs and skill links, asks before removing them, then installs this checkout's hooks and skills. When Lite's Copilot folder setting still points at the old folder, it offers to repoint that one field through Lite's Settings API.

   **Check:** `[ok] old hook config removed` for each old file, `[ok] Copilot folder setting -> ... (read back from Lite)` or `already points here`, and no `[warn]` line about two session-state hooks.
4. `.\tray.bat --restart`, start the chief from the Board, and restart any open Copilot session.

## 3. A machine that already has its own skills repo

Use this when the machine keeps its own `issue-*` skills in a repo of its own, so they match its forge and its habits.

1. Complete steps 1 to 3 of the fresh-machine case, but **answer `n`** when `setup.bat` offers the Copilot install.
2. Set Settings > Chief > **Skills folder** to the folder that holds `issue-add/`, `issue-start/`, `issue-yolo/` and `issue-finish/`.

   **Check:** the line under it reads `found (from the skills folder)` for all four. A `missing` line names the folder it looked in.
3. **Preview** what the install would do:

   ```powershell
   .\.venv\Scripts\python.exe -m src.launching.ps_script copilot\install.ps1 -WhatIf
   ```

   Read every `[whatif]` line. Your existing skills, hooks and links show as `[ok] kept (not Lite's)`. When `~/.copilot/skills` or `~/.claude/skills` is itself a link into another repo, the preview says so and Lite writes nothing inside it.
4. **Install:**

   ```powershell
   .\.venv\Scripts\python.exe -m src.launching.ps_script copilot\install.ps1
   ```

   **Check:** `[ok] issue skill linked -> ...` for each of the four that was not already there, and a run straight after reports `[ok] unchanged`. The links it makes into your skills repo are listed in `~/.copilot/app-launcher-lite-install.json`, which is how uninstall knows they are Lite's.
5. Start the chief and run one test lane, as in the fresh-machine case.

Clearing the Skills folder later and running the install again removes Lite's links into your repo and puts the bundled copies back.

## 4. Uninstall

```powershell
.\.venv\Scripts\python.exe -m src.launching.ps_script copilot\install.ps1 -Uninstall -WhatIf
.\.venv\Scripts\python.exe -m src.launching.ps_script copilot\install.ps1 -Uninstall
```

It removes only what is Lite's: the two `app-launcher-lite-*.json` hook configs that run this checkout's hooks, the skill links into this checkout, and the links into your skills folder that it recorded and that still point where it recorded them. It never deletes inside a linked folder, never touches `hooks/state`, and never edits a config file. Restart any open Copilot session afterwards.

**Check:** each item reads `[ok] ... removed` or `[ok] not installed`, and a second run removes nothing.

## Config files

- `config/webapp_config.json`: the installer adds `skills_dir` (empty) only when the key is missing. It writes a timestamped backup next to the file first (`webapp_config.json.bak-<date>-<time>`) and prints the key it added. It never changes an existing value. A file that does not parse is reported as `[unknown]` and left untouched.
- `config/config.json` is committed with the repo and is never edited by the installer.
