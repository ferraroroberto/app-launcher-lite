"""Per-agent command-line composition — the launch shape of every coding agent.

Split off ``src/webapp_config.py``. That module owns *configuration*: the
:class:`~src.webapp_config.WebappConfig` dataclass plus its load / save /
validate plumbing. What lives here is not config plumbing at all — it is the
business logic that turns those persisted knobs into the exact argv each
agent's CLI expects, and it is the part that changes when a new harness is
onboarded, not when a setting is added.

One builder per registered agent, plus the resume-launch variant:

* :func:`build_claude_flags`, :func:`build_antigravity_flags`,
  :func:`build_codex_flags`, :func:`build_copilot_flags`,
  :func:`build_pi_flags`, :func:`build_grok_flags` — the normal launch line.
* :func:`build_resume_flags` — the same, with the agent's native resume token
  spliced ahead of the flags its resume path actually accepts.

**Adding a seventh coding agent?** This is the module the flag-builder step of
`fleet-config`'s ``docs/adding-a-coding-harness.md`` lands in; the allowed
values it validates against (``VALID_*``, ``PI_MODEL_SPECS``) stay in
``webapp_config.py`` beside the dataclass field and its validator.

Imports one-way (``src.launching.flags`` → ``webapp_config``): the config module never
imports this one, so there is no cycle to work around.
"""

from __future__ import annotations

from typing import Optional

from src.agents import resume_command_for
from src.launching.model_catalog import (
    CLAUDE_MODEL_SPECS,
    CODEX_MODEL_SPECS,
    LEGACY_CLAUDE_DEFAULT_EFFORTS,
    PI_MODEL_SPECS,
)
from src.webapp_config import (
    ALWAYS_ON_CLAUDE_FLAGS,
    DEFAULT_CLAUDE_EFFORT,
    DEFAULT_PI_MODEL,
    VALID_CLAUDE_MODELS,
    VALID_CODEX_MODELS,
    VALID_GROK_EFFORTS,
    VALID_PI_EFFORTS,
    VALID_PI_MODELS,
    WebappConfig,
    valid_claude_efforts_for,
)

# The Claude Code Telegram channel plugin. ``--channels`` makes Claude
# start that plugin's server and inject the chat's messages into the session.
CHANNEL_PLUGIN = "plugin:telegram@claude-plugins-official"


def default_effort(agent: str, model: str) -> str:
    """The catalog ``default_effort`` of ``model``, else ``high``."""
    if agent == "claude":
        spec = CLAUDE_MODEL_SPECS.get(model, {})
        default = spec.get("default_effort") or LEGACY_CLAUDE_DEFAULT_EFFORTS.get(model)
    elif agent == "codex":
        default = CODEX_MODEL_SPECS.get(model, {}).get("default_effort")
    elif agent == "pi":
        default = PI_MODEL_SPECS.get(model, {}).get("default_effort")
    else:
        raise ValueError(f"no per-model effort for agent {agent!r}")
    return default or DEFAULT_CLAUDE_EFFORT


def effective_effort(agent: str, model: str, cfg: WebappConfig) -> str:
    """The effort ``model`` launches at: the owner's override for that
    model, else the catalog ``default_effort``, else ``high``.

    The one copy of the precedence rule; every builder asks it for the model it
    is actually launching, never the selected one. An override the model does
    not support is ignored rather than trusted, and an unknown model resolves
    to ``high``. For Claude the value may be ``off`` (an override meaning "send
    no ``--effort``"); the builder, not this function, acts on it. For Pi the
    value is a ``--thinking`` level (``off`` included) sent as is.
    """
    if agent == "claude":
        overrides, allowed = cfg.claude_model_efforts, valid_claude_efforts_for(model)
    elif agent == "codex":
        overrides = cfg.codex_model_efforts
        allowed = CODEX_MODEL_SPECS.get(model, {}).get("efforts", ())
    elif agent == "pi":
        overrides, allowed = cfg.pi_model_efforts, VALID_PI_EFFORTS
    else:
        raise ValueError(f"no per-model effort for agent {agent!r}")
    override = overrides.get(model)
    return override if override in allowed else default_effort(agent, model)


def claude_model_family(recorded: str) -> str:
    """The Claude model a recorded id belongs to (``claude-opus-5-5`` → ``opus``),
    or ``""`` when it names none of ours."""
    lowered = (recorded or "").lower()
    return next((m for m in VALID_CLAUDE_MODELS if m in lowered), "")


def build_claude_flags(
    cfg: WebappConfig,
    model_override: Optional[str] = None,
    *,
    channel_settings: Optional[str] = None,
    effort_model: Optional[str] = None,
    effort_override: Optional[str] = None,
) -> str:
    """Compose the `claude` CLI flags from the persisted defaults.

    ``model_override`` forces a specific ``--model`` regardless of the
    persisted ``claude_model`` — used by the Life OS tab,
    whose ``opus`` toggle picks ``opus``/``sonnet`` per launch while the
    rest of the flags (verbose, debug, permission) still come from the
    shared Coding options. Other callers pass nothing and keep the persisted
    model.

    ``--effort`` is that model's own (:func:`effective_effort`): the
    model actually launched, never the selected one. When no valid model is
    sent (a keep-model resume leaves the model to the conversation), the
    effort is resolved for ``effort_model`` — the conversation's recorded
    model id — and omitted when that names none of ours, which leaves the
    model at its own default.

    ``effort_override`` replaces that resolved effort for one launch (a Board
    dispatch's ``effort``); the caller has already checked the model offers
    it. ``None`` keeps the persisted per-model effort.

    ``channel_settings`` — the path of a per-profile settings file — turns the
    session into a Telegram channel session: ``--channels`` starts the
    plugin and ``--settings <file>`` hands it its ``TELEGRAM_STATE_DIR``
    through Claude's own settings ``env`` block, so the variable reaches that
    session's plugin server and no other. A channel session never runs in
    bypass mode, whatever the shared Coding option says.
    ``None`` (every other caller) leaves the output exactly as before.
    """
    parts: list[str] = list(ALWAYS_ON_CLAUDE_FLAGS)
    if cfg.claude_permission_mode == "skip" and not channel_settings:
        parts.append("--dangerously-skip-permissions")
    else:
        parts.extend(["--permission-mode", "auto"])
    model = model_override if model_override is not None else cfg.claude_model
    if model in VALID_CLAUDE_MODELS:
        parts.extend(["--model", model])
    effort_target = model if model in VALID_CLAUDE_MODELS else claude_model_family(effort_model or "")
    if effort_target:
        effort = effort_override or effective_effort("claude", effort_target, cfg)
        if effort != "off":
            parts.extend(["--effort", effort])
    if cfg.claude_verbose:
        parts.append("--verbose")
    if cfg.claude_debug:
        parts.append("--debug")
    if channel_settings:
        parts.extend(["--channels", CHANNEL_PLUGIN, "--settings", channel_settings])
    return " ".join(parts)


def build_antigravity_flags(cfg: WebappConfig) -> str:
    """Compose the `agy` CLI flags from the persisted Antigravity toggles.

    The Antigravity CLI (`agy`) does have `--model` and `--effort`, but its
    model is chosen in-session with `/model`, so the launcher has no model to
    key an effort on and sends neither. It has no
    verbose flag. So this is just the two opt-in launch switches; an
    all-default config yields an empty string (the CLI is launched bare).
    """
    parts: list[str] = []
    if cfg.antigravity_skip_permissions:
        parts.append("--dangerously-skip-permissions")
    if cfg.antigravity_sandbox:
        parts.append("--sandbox")
    return " ".join(parts)


def build_codex_flags(
    cfg: WebappConfig, model_override: Optional[str] = None,
    *, effort_override: Optional[str] = None,
) -> str:
    """Compose the `codex` CLI flags from the persisted Codex knobs.

    Three pieces: explicit model, permission mode, and a model-compatible
    reasoning tier (``effort_override`` for one launch, already checked
    against the model, else the persisted per-model effort). The reasoning value is sent bare: it isn't valid TOML, so
    Codex's `-c` parser falls back to the raw string — which also dodges
    Windows ``cmd`` quote-stripping that a quoted value would suffer.
    """
    parts: list[str] = []
    model = model_override if model_override is not None else cfg.codex_model
    if model in VALID_CODEX_MODELS:
        parts.extend(["--model", model])
    if cfg.codex_permission_mode == "skip":
        parts.append("--dangerously-bypass-approvals-and-sandbox")
    else:
        parts.extend(
            ["--ask-for-approval", "never", "--sandbox", "workspace-write"]
        )
    if model in VALID_CODEX_MODELS:
        effort = effort_override or effective_effort("codex", model, cfg)
        parts.extend(["-c", f"model_reasoning_effort={effort}"])
    return " ".join(parts)


def build_copilot_flags(
    cfg: WebappConfig,
    model_override: Optional[str] = None,
    effort_override: Optional[str] = None,
) -> str:
    """Compose the `copilot` CLI flags from the persisted Copilot knobs.

    - ``--model`` is emitted only for a non-empty explicit id ("" and the
      "default" sentinel both mean "let Copilot pick auto" → omit). Explicit
      ids are gated by the account's plan: Copilot falls back to auto and
      prints one line when an id is not available, so ``copilot_models`` is
      a per-install list rather than a catalogue.
    - ``--effort`` is emitted only alongside an explicit model: the auto
      model rejects it ("does not support reasoning effort configuration").
    - ``--context`` is emitted for "default"/"long_context" (omit on "").
    - ``--autopilot`` and ``--allow-all`` follow their switches.

    ``model_override`` forces a per-launch model (Team OS, Board, chief) and
    follows the same rules: an empty/"default" override means no ``--model``
    and no ``--effort``. ``effort_override`` replaces the saved effort for
    one launch.
    """
    parts: list[str] = []
    if cfg.copilot_skip_permissions:
        parts.append("--allow-all")
    raw = model_override if model_override is not None else cfg.copilot_model
    model = "" if raw in ("", "default") else raw
    if model:
        parts.extend(["--model", model])
    if cfg.copilot_autopilot:
        parts.append("--autopilot")
    if cfg.copilot_context in ("default", "long_context"):
        parts.extend(["--context", cfg.copilot_context])
    effort = effort_override or cfg.copilot_effort
    if model and effort:
        parts.extend(["--effort", effort])
    return " ".join(parts)


def build_pi_flags(cfg: WebappConfig) -> str:
    """Compose the `pi` CLI flags from the persisted Pi knobs.

    Three pieces, all passed explicitly because pi's settings.json defaults
    do not reliably reroute a launch:

    - **provider + model** — looked up from
      :data:`src.launching.model_catalog.PI_MODEL_SPECS` for the chosen option.
      Opus/Sonnet route to the ``claude-agent-sdk`` provider (the Claude
      **subscription** quota, no API "extra usage" credits); GPT routes to
      ``openai-codex`` (the ChatGPT-plan subscription). ``pi_model`` is never
      empty — an unknown value falls back to ``DEFAULT_PI_MODEL`` so the launch
      can't slip onto a billing path (pi's native ``anthropic`` provider is
      deliberately bypassed, and disconnected on this machine).
    - **thinking** — ``--thinking <effort>``: the chosen model's own
      (:func:`effective_effort`), its override else ``high``.
    - **trust** — ``--approve`` (trust mode) or ``--no-approve`` (ask mode).
      This is project *trust* (loading project-local ``.pi/`` resources), NOT
      a tool-permission gate: pi has no tool sandbox or per-action prompt.

    In-session switching stays available via ``/model`` / ``Ctrl+L`` /
    ``Shift+Tab``. See docs/pi-coding-agent.md.
    """
    model = cfg.pi_model if cfg.pi_model in VALID_PI_MODELS else DEFAULT_PI_MODEL
    spec = PI_MODEL_SPECS[model]
    provider, model_arg = spec["provider"], spec["model_arg"]
    parts = ["--provider", provider, "--model", model_arg]
    parts.extend(["--thinking", effective_effort("pi", model, cfg)])
    parts.append("--approve" if cfg.pi_trust_mode == "trust" else "--no-approve")
    return " ".join(parts)


def build_grok_flags(
    cfg: WebappConfig,
    *,
    effort_override: Optional[str] = None,
    unattended: bool = False,
) -> str:
    """Compose the `grok` CLI flags from the persisted Grok knobs.

    Two pieces, both verified against the running binary rather than the
    docs (0.2.114) — Grok's properties are probed,
    not assumed:

    - **permission mode** — ``--permission-mode auto`` or
      ``--permission-mode bypassPermissions`` for "skip". Auto still opens a
      prompt in an interactive session when its check will not pass a call,
      so an unattended launch (nobody there to answer) always sends
      bypassPermissions. Deny hooks still refuse a blocked command. The
      Coding tab and Resume keep the saved mode.
    - **reasoning tier** — ``--reasoning-effort <low|medium|high>``, the
      exact set the CLI's own rejection message names.

    No ``--model``: a launch runs the account default, which ``grok models``
    marks as the current model, and the Board catalog names that same model
    (``GROK_DISPATCH_MODEL_SPECS``). The CLI's ``/model`` switches in-session.

    ``effort_override`` (one of ``VALID_GROK_EFFORTS``) replaces the
    persisted tier for one launch. ``unattended`` forces bypassPermissions
    for that one launch.
    """
    skipped = unattended or cfg.grok_permission_mode == "skip"
    parts = [
        "--permission-mode",
        "bypassPermissions" if skipped else "auto",
    ]
    effort = effort_override or cfg.grok_effort
    if effort in VALID_GROK_EFFORTS:
        parts.extend(["--reasoning-effort", effort])
    return " ".join(parts)


def build_resume_flags(
    cfg: WebappConfig, agent_id: str, model_override: Optional[str] = None,
    session_id: Optional[str] = None, *, keep_model: bool = False,
    effort_model: Optional[str] = None, effort_override: Optional[str] = None,
) -> str:
    """Compose the full flags string for a *Resume* launch.

    Splices the agent's native resume token (see
    :func:`src.agents.resume_command_for`) ahead of the flags its resume
    path actually accepts, so the launch line becomes
    ``<command> <resume-token> <flags>`` and the agent renders its own
    session picker over the PTY.

    Most agents accept their normal launch flags after the resume token,
    so this is ``<token> <normal builder output>``. The one exception is
    **Codex**: its ``resume`` subcommand rejects the top-level
    ``--ask-for-approval`` / ``--sandbox`` switches, accepting only the
    config override — so a Codex resume carries just
    ``resume -c model_reasoning_effort=<effort>``.

    ``model_override`` forces a specific Claude or Codex ``--model``. Life OS
    uses only the Claude path; Coding and Board can override either provider.

    ``session_id``, when given, pins the resume token to that specific
    conversation (``<token> <session_id> <flags>``) so the agent reattaches
    directly instead of rendering its interactive picker (the
    fleet chief's Resume control — verified empirically that Claude's
    ``--resume <id>`` accepts a session id directly, non-interactively).
    Omitted (the default), the bare token
    renders the picker exactly as before.

    ``keep_model`` (Life OS's swipe-Resume) resumes a Claude
    conversation on its own model: no ``--model`` at all, because a bare
    ``--resume <id>`` keeps the conversation's model and ``--model`` would
    override it (probed, see :mod:`src.session_view.conversation_model`). Codex has no such
    memory, so a Codex caller passes the recorded model as ``model_override``
    and ``keep_model`` changes nothing there. An absent model is never read as
    "keep": that means the persisted default, as it always has.

    ``effort_model`` (Claude, with ``keep_model``) is the conversation's
    recorded model id: no ``--model`` is sent, so the effort is resolved for
    that model's family, and omitted when it is unknown.

    ``effort_override`` is one launch's effort (a launch preset's), already
    checked against the model; Claude and Codex carry it, ``None`` keeps the
    saved per-model effort.
    """
    token = resume_command_for(agent_id)
    if session_id:
        token = f"{token} {session_id}"
    if keep_model and agent_id == "claude":
        model_override = ""  # not a valid model, so build_claude_flags adds no --model
    if agent_id == "codex":
        parts = [token]
        model = model_override if model_override is not None else cfg.codex_model
        if model in VALID_CODEX_MODELS:
            parts.extend(["--model", model])
        if model in VALID_CODEX_MODELS:
            effort = effort_override or effective_effort("codex", model, cfg)
            parts.extend(["-c", f"model_reasoning_effort={effort}"])
        return " ".join(parts).strip()
    if agent_id == "claude":
        base = build_claude_flags(
            cfg, model_override=model_override, effort_model=effort_model,
            effort_override=effort_override,
        )
    elif agent_id == "antigravity":
        base = build_antigravity_flags(cfg)
    elif agent_id == "copilot":
        base = build_copilot_flags(
            cfg, model_override=model_override, effort_override=effort_override,
        )
    elif agent_id == "pi":  # keep the SDK provider/model on resume
        base = build_pi_flags(cfg)
    elif agent_id == "grok":  # no launch knobs — bare `--resume`
        base = build_grok_flags(cfg)
    else:
        base = ""
    return f"{token} {base}".strip()
