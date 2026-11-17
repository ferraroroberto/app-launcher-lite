# Installing the CLIs

The launcher drives one coding agent, the GitHub Copilot CLI, and reads one forge, GitLab, through `glab`. Install both on the machine that runs the launcher. Back to [Quick start](../README.md#quick-start).

## Installing the GitHub Copilot CLI

The Coding tab and the Team OS tab launch the **GitHub Copilot CLI** (`copilot`), GitHub's terminal-native agentic coding agent. The Coding tab's launch button stays disabled until `copilot` is on `PATH`. Install it with WinGet:

```powershell
winget install -e --id GitHub.Copilot
```

It is also available through npm (`npm install -g @github/copilot`, needs Node.js 22+) and a standalone installer; see the [official docs](https://docs.github.com/copilot/how-tos/set-up/install-copilot-cli) for the channel that suits you. Verify with `copilot --version`.

> **Authentication is not the launcher's job.** The CLI signs in *inside the session*: run `/login` at the `copilot` prompt and follow the on-screen instructions; it needs an active GitHub Copilot subscription. The launcher only resolves the `copilot` binary and spawns it.

Keep it current with:

```powershell
copilot update
```

Hook configuration (fleet-config-lite's `install.ps1`) loads at CLI startup only, so restart any open Copilot session after an install or an update, and re-run the hook check from the README's [deploy steps](../README.md#deploying-to-another-computer) after each update.

`copilot` is resolved against the **effective** `PATH` (the inherited environment plus the machine and user registry values), so installing it enables the launch button on the next detection poll without restarting anything. Only a change to `src/agents.py` itself needs a restart, and because the `:8466` session-host imports that module at its own start, that restart is the one described in [restart-and-liveness.md](restart-and-liveness.md).

Which model ids an account may use depends on its plan; edit `copilot_models` in `config/webapp_config.json` to the real set (see [Copilot models and flags](../README.md#copilot-models-and-flags)).

## Installing glab

The Board reads GitLab through the `glab` CLI, as a subprocess, on demand only. Install it with WinGet:

```powershell
winget install -e --id GitLab.GLab
```

Other channels (Scoop, Chocolatey, a release archive) are listed in the [glab repository](https://gitlab.com/gitlab-org/cli). Verify with `glab --version`.

Sign in once, as the owner, in a terminal on the machine that runs the launcher:

```powershell
glab auth login
```

Pick your GitLab host (gitlab.com or a self-hosted instance) and follow the prompts. The token stays in glab's own credential store; nothing in this repo holds it, and none belongs in a config file, an issue or a commit.

Then set `gitlab_group` (and `gitlab_host` for a self-hosted instance) in Settings. Everything the Board does with GitLab is **not yet verified live**; the details and the first check to run are in [gitlab.md](gitlab.md).
