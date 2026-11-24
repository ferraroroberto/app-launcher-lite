# Migrating from fleet-config-lite

App Launcher Lite carries its own Copilot hooks, chief helpers and issue skills in [`copilot/`](../copilot), and installs them with `copilot/install.py`. A machine that already runs the companion repo fleet-config-lite has the same hooks and skills installed from *that* checkout. Two copies of the session-state hook would race on one state file, so the old install is removed first. This page is the order to do it in.

A machine that never ran fleet-config-lite needs none of this: `setup.bat` offers the install, or run it by hand (see [Install, uninstall, migrate](#install-uninstall-migrate)).

## What moves and what does not

| | Moves? |
|---|---|
| `~/.copilot/hooks/state/` (session state, the chief's plan, journal, handover and mailbox) | Moved to `~/.copilot/app-launcher-lite-state/` by `--migrate`, every file kept, so Copilot CLI stops reading it as hook configs. The chief resumes from it. How the move works with a chief live: [install.md](../copilot/docs/install.md#5-a-machine-whose-state-is-still-in-hooksstate). |
| The two hook configs `fleet-config-lite-session-state.json` and `fleet-config-lite-chief-guard.json` in `~/.copilot/hooks/` | Removed, replaced by `app-launcher-lite-session-state.json` and `app-launcher-lite-chief-guard.json`, which run this checkout's `copilot/hooks/`. |
| Skill junctions in `~/.copilot/skills/` that point into a `fleet-config-lite` checkout | Removed, replaced by junctions into `copilot/skills/`. |
| Anything else in `~/.copilot` (another tool's skills, a hand-written hook config, `copilot-instructions.md`) | **No.** Only entries that carry Lite's own marker, or the old install's, are touched. |
| The fleet-config-lite checkout itself | **No.** Nothing reads it afterwards; delete it when you like. |
| `~/.claude/settings.json`, agent trust settings, git config | **Never** written by the installer. |

## Order

The order matters because the chief's folder is a setting. Lite defaults it to its own `copilot/` only while it is empty; a machine whose Settings hold an explicit path (the old sibling folder) keeps it, and a chief started there is not recognised once Lite looks in `copilot/`.

1. **Stop the chief at a turn end.** Wait until it is idle (a **Your turn** card, not mid-turn), then stop it from the Board. Its plan and handover move with the state folder and are all kept.
2. **Update Lite:** `git pull --ff-only` in this checkout. Leave the tray running: step 3 reaches Lite's Settings API.
3. **Migrate:**

   ```powershell
   .\.venv\Scripts\python.exe copilot\install.py --migrate
   ```

   It lists the old hook configs and skill links it would remove and asks before touching anything; `-Yes` answers for it. Then it installs this checkout's hooks and skills. It also reads Lite's **Copilot folder** setting: when that holds a path other than this checkout's `copilot/`, it offers to point it there through Lite's own Settings API, changing that one field and reading it back. If Lite is not running, or does not answer, the line reads `[unknown]` and nothing is changed: start the tray and run the command again, or set the folder in **Settings → Folders** yourself.
4. **Restart Lite:** `tray.bat --restart`. The session-host on `:8466` is not part of it and keeps your live sessions.
5. **Start the chief from the Board.** It now runs in `copilot/` and picks up the plan where it stopped.
6. **Restart any open Copilot session.** Hook configs load at CLI startup only.

Run the commands from this checkout's root. The installer is plain Python, so it runs under every execution policy, `AllSigned` included, without changing the machine's policy; on a machine where scripts may run, `.\copilot\install.ps1 -Migrate` forwards to the same code.

## Checking it worked

- `~/.copilot/hooks/` holds the two `app-launcher-lite-*.json` files and no `fleet-config-lite-*.json`; `~/.copilot/skills/` junctions point into this checkout's `copilot/skills/`.
- **Settings → Folders** shows this checkout's `copilot/` as the Copilot folder.
- `~/.copilot/app-launcher-lite-state/sessions-state.json` keeps updating as sessions start and stop, and the Board shows them.
- Running the command again reports nothing left to remove: it is idempotent.

## Install, uninstall, migrate

```powershell
.\.venv\Scripts\python.exe copilot\install.py             # install or refresh
.\.venv\Scripts\python.exe copilot\install.py --uninstall  # remove what this installer put there
.\.venv\Scripts\python.exe copilot\install.py --migrate    # leave fleet-config-lite, then install
```

Install is idempotent: re-run it after moving the checkout or changing a hook template. `--uninstall` removes only Lite's own hook configs and skill links, never the state folder, and restores nothing, so to go back to fleet-config-lite run `--uninstall` here and then that repo's own `install.ps1`; never leave both installed.

**A shared machine** is one where `~/.copilot/skills` or `~/.agents/skills` is a link into another tree, so the full fleet runs there too. The installer then writes no skill, and renders the session-state hook with `--lite-sessions-only`, so it records only sessions Lite launched. A hook config, hooks folder or skills folder that is itself a link into another tree is never written through.

**Global instructions.** fleet-config-lite also linked a `copilot-instructions.md`. This checkout does not manage global instructions, so a migrated machine keeps whatever file is there; remove it yourself if you no longer want it.

## If it goes wrong

- `[warn] the old fleet-config-lite hook configs are still installed` after a plain install: run it again with `--migrate`. Until then both session-state hooks write the same file.
- `[skip] skill exists and is not ours`: a real folder, or a link into another tree, already holds that skill name. Move it aside and re-run.
- A refused prompt (`no answer to read`) means there was no terminal to ask on; re-run with `-Yes`.
- Where the Copilot folder setting stays on the old path, start the chief only after fixing it in **Settings → Folders**.
