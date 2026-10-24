# The launcher-owned PTY terminal

The architecture, security model, and hard-won gotchas behind the launcher-owned ConPTY terminal — a full interactive terminal on the phone, and why each piece is shaped the way it is. Read this before extending the terminal feature.

## 1. Goals and constraints

The launcher owns its ConPTY sessions instead of spawning a detached CMD window it cannot see, and puts a **full interactive terminal on the phone** on top: live output, scrollback, typing, `Ctrl+C`, `/exit`, image paste. The constraint that shapes everything below: the terminal must be at least as safe as the cloud surface the app already exposes, ideally safer.

## 2. The architecture, and why each piece exists

- **A separate `session-host` process owns the PTYs.** The obvious design is "the webapp owns the ConPTYs." It's wrong: every *Restart webapp* would kill every running agent session. Keeping the PTYs in their own long-lived process (loopback-only, port `8466`, owned by the tray like `cloudflared`) lets the streaming layer restart without touching the work. **Rule: separate the thing that restarts often from the thing that must not die.**
- **The webapp is the single auth choke point.** WebSockets *bypass HTTP middleware* — the bearer-token middleware never runs for a `ws://` upgrade. So every terminal-grade WS route re-applies the full gate (edge header, private network, bearer, passkey token) through one shared `middleware.ws_terminal_gate()`, called right after `accept()`. **Rule: when you add an auth middleware, audit every protocol that skips it.**
- **The session-host fans output to N subscribers.** Output goes through a ring buffer plus per-client queues, so a second viewer such as the PC mirror window is just another subscriber rather than a special case.

## 3. Security: the model and the reasoning

The threat model: the risk is the one already present — anyone on the tailnet could already RDP into the PC. That is why **Tailscale-only** is the foundation, not an afterthought. But "no worse than today" isn't the bar for a surface that can run the agent with permission prompts disabled (Copilot's `--allow-all`), so the gate is layered:

1. **Tailscale-only** — reject the `Cf-Ray` / `Cf-Connecting-IP` headers (public Cloudflare tunnel) and require the client IP in the `100.64.0.0/10` CGNAT range, loopback, or an explicit allowlist.
2. **Bearer token** — same as the rest of the app.
3. **WebAuthn platform passkey (device passkey, unlocked with the phone's screen lock or biometric)** — an enrolled-device whitelist. A passkey assertion mints a short-lived (12 h) terminal token; the WS and image endpoints demand it.
4. **Audit log** — every session start/stop, WS open/close, input, image, plus a full per-session transcript.

**Each layer assumes the others failed.** The passkey gate does not stop an attacker who is already on the tailnet *and* has the bearer token and the enrolled phone; it stops the case where they have the token but not the phone.

**The deliberate exception:** loopback clients (the PC itself) skip the passkey — the phone's passkey isn't on the PC, and loopback already implies you're at the machine. The bypass is scoped to loopback, narrower than the gate it skips, and documented here on purpose.

## 4. Gotchas and the rule each one encodes

### A correct security boundary can look like a bug

A phone on the **public Cloudflare tunnel** hitting the Tailscale-only terminal is rejected correctly, but a WS closed *before* `accept()` shows the browser only close code `1006` with no reason — a bare "Disconnected." Two rules follow:

- **Accept the WebSocket first, then close with a code + reason.** Closing before the handshake completes gives the client nothing to display.
- **A correct rejection still needs a good error.** `/api/status` reports `terminal.reachable` and `terminal.reason`, and the UI runs a pre-flight check so it explains *"open me over the Tailscale URL"* instead of failing mute. Security that the user experiences as a random bug erodes trust in the security.

### Behaviour that differs between the tray and a terminal

The tray's Tailscale-URL lookup depends on three environment facts: (1) the venv `pythonw.exe` is a **redirector stub** — every launch is one idle stub plus one real process; (2) the `tailscale` CLI is **not necessarily on `PATH`** (it lives in `C:\Program Files\Tailscale\`), so the lookup probes Program Files; (3) startup-path subprocesses get generous timeouts, because a short one fails under tray-startup load. **Rules: never assume a CLI is on `PATH`; give startup-path subprocesses generous timeouts; when a process behaves differently by launcher, suspect the environment, not the code.** The lookup writes `webapp/tailscale_debug.log` and surfaces the failure reason in the notification, because diagnosing this blind is expensive.

### One PTY has exactly one size

When the phone and the PC mirror both attach to the same ConPTY, they **cannot** each have their own layout — a pseudo-console has a single `rows × cols`, and the last client to call `resize()` wins. There is no clever fix; you **pick an authority**. The phone is the sole size authority: the WS proxy tags each client `role=pc|phone`, the session-host honours `resize` only from `phone`, and the PC window *mirrors* whatever size the phone set. **Rule: when a resource is fundamentally single-valued, don't simulate sharing — assign ownership.**

### Detached is not untracked

A detached-mode session is a CMD window the launcher can still kill without holding a child handle. The console is spawned through a transient PowerShell `Start-Process -PassThru` that **re-parents it out of the session-host's process tree** — so a `tray.bat --restart`, which tears the tray subtree down with `taskkill /T`, can't cascade into it — and the launcher keeps **only the PID** that `-PassThru` printed. That PID is enough to **list and kill** it (`taskkill /PID … /T /F`); liveness is a bare PID probe, not a `Popen`. **Rule: "detached" and "untracked" are different choices — the window keeps its independence (it survives a launcher restart) and the launcher keeps a kill switch.**

### Self-signed certs and loopback

The PC mirror window (an Edge/Chrome `--app` window over `https://127.0.0.1`) hits a cert error: the cert's SAN is the `.ts.net` hostname, not `127.0.0.1`, and the CA isn't trusted on the PC. The window is launched with `--ignore-certificate-errors --test-type`, **safe specifically because it only ever points at our own loopback origin**. **Rule: "ignore cert errors" is a scoped tool — the question is always *which* origin.**

## 5. Working practices

- **Cheap gates catch most "it doesn't even start" bugs.** `py_compile`, `node --check`, an import-and-`create_app()` smoke test and `curl /healthz` each take seconds; the pre-ship gate (`scripts/verify-before-ship.ps1`) runs the full set.
- **Cache-busting is automatic.** Static asset URLs carry a content hash (`?v=<hash>`) stamped at serve time, so any edit busts the phone's cache with no manual step — see `docs/cache-hygiene-asset-hashing.md` (and do **not** reintroduce hand-bumped `?v=N` query strings). Mobile browsers cache aggressively, so a hand-bumped version makes "the user is testing stale code" a costly confusion; the content hash removes it.
- **The reference doc lives next to the code.** `docs/` captures durable design rationale; update it in the same change as the code to keep it honest.
- **Diagnose from the audit log.** The client IP in the audit log tells you which edge a client came through (a public IP means the Cloudflare tunnel), so the logging built for security doubles as the first debugging tool.

## 6. If you extend this

- **Per-client rendering** would need the session-host to broadcast size-change frames so a mirror can letterbox/scale instead of just matching. We chose not to — "phone drives, PC mirrors" was enough.
- **Detached sessions survive a `tray.bat --restart`.** The console is re-parented out of the tray's process tree and the session-host (`:8466`) is excluded from the restart's reclaim sweep, so the fresh tray re-adopts the still-running session-host and its in-memory list — the detached window keeps running and stays listed (`alive` is a PID probe). The gap left: a genuine *session-host process* restart still drops them from the list — the orphaned console keeps running but its PID is no longer tracked. Re-discovery by process scan would close that gap; it wasn't worth the complexity.
- **The terminal token TTL is 12 h.** If you shorten it, add a quiet re-auth path so the user isn't bounced mid-session.
- **Edit mode** gates per-row rename/remove behind a Settings toggle to keep the lists icon-free. If you add more per-row actions, put them there too — resist icon inflation.
