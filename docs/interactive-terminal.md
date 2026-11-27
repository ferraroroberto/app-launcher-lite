# Interactive terminal from the phone

Launching a Coding-tab project in **full control** (the default; the Detached switch off) opens a **live terminal**: what you would see in the console window on the PC, streamed to the phone — real output, scrollback, typing, `Ctrl+C`, `/exit` and image paste. Tap a full-control session in the list to re-attach. The terminal is the **Terminal mode** of the [session view](session-view.md).

A full-control launch from the **phone** opens the terminal in a dedicated Edge `--app` window on the PC as well, so it closes independently of your other tabs. A launch from a desktop browser on the PC opens that window only with **Settings › Sessions › Open in a new window** (`open_sessions_in_window`, off by default); off, the session opens in the page.

The loopback-only session-host also exposes the same ConPTY/WebSocket engine to trusted sibling services: `POST http://127.0.0.1:8466/sessions` with `{"kind":"pty","agent":"ssh","flags":"user@host","project_dir":"<existing-dir>","cols":120,"rows":30}` starts `cmd /c ssh user@host`, and the returned `session_id` uses the existing `/sessions/{id}/ws` input/resize/raw-output/shutdown protocol. SSH is session-host-only and is not a Coding-tab agent.

A Board session whose issue title is known before the agent starts is named at spawn through Copilot's documented `--name` flag, which syncs the native resume picker without interacting with the live TUI. A title unsafe to pass through `cmd.exe` keeps the launcher's own label.

## Terminal toolbar and composer

A small bar above the terminal covers what a phone keyboard cannot do well, and the **composer** is always docked under it: a tall predictive `<textarea>` plus a button grid (**⌨️ keys**, **🖼 image · 📎 attach**, **➤ send**). It is a real textarea because xterm.js wipes its own helper textarea, which breaks a predictive keyboard. The PC mirror window docks it in its Terminal view as well, so Chat and Terminal offer the same message box there; the mirror still never resizes the PTY.

The terminal follows its box: a `ResizeObserver` on the host re-runs sizing when the composer grows or clears, as well as on viewport changes, and the last row always ends above the composer. A full-screen agent (Copilot) keeps its PTY columns pinned and its rows never shrink, so a smaller box (keyboard up, tall composer) pans the canvas up instead of repainting the agent; a box that outgrows the canvas, such as a terminal first opened with the keyboard up, grows the rows once to fill it.

One PTY has one grid, and a desktop browser's in-page terminal sizes it as a phone does. Output laid out for the desktop's width reads as scattered fragments in the phone's narrower grid, so a phone showing the Terminal view compares the size in the sessions list (the title poll it already runs, and at once when the view is shown again) with its own and sends its own again when they differ; the agent then repaints for the phone. The desktop browser shows that same narrower frame while the phone is watching. A phone does this at most once per 30 s without being brought back to the front, so two phones do not trade the size on every poll.

- **⋮ Session menu** — the bar's last control, keeping the bar at five controls on a 390 px phone (‹ Back · title · context ring · mode toggle · ⋮): **Rename**, **Copy link**, **Changed files**, **Links**, **Compact** (a Copilot session), **Stop and kill**. **Copy link** gives the provider's own session URL when there is one, else the launcher link ([which link](session-view.md#copy-link)).
- **Model chip** — names the model a Copilot session runs: the newest `session.start` / `session.model_change` record in its `events.jsonl`, else its `--model` flag. A tap opens **Switch model**, one row per id on `copilot_models`. A pick types Copilot's own `/model <id>`, which applies to that session only (the Settings default and Copilot's saved default stay), and the chip changes only once the event log records the new model. The switch is refused, with nothing typed, while Copilot is working (it would queue the command), while its prompt holds text or a menu is open, and before the session's first prompt, because Copilot writes no event log until then.
- **Context ring** — Copilot's own context-window figure, the `(NN%)` in the footer under its prompt, read off the session-host's VT mirror of the screen (the PTY capture holds only partial repaints). A terminal too narrow for the whole status line (a phone at about 46 columns) cuts the figure off, and the ring then stays hidden: nothing else Copilot writes states the window's use, so no figure is computed in its place. A tap opens the Session usage sheet. When a Copilot footer shows `Session: N AIC used`, the figure also rides in a label beside the ring (`9% · 5.6 AIC`), on the sheet and on the Code tab's session row; it is the screen's own number (`src/session_view/copilot_credits.py`), the Code tab's copy refreshed in the background at most every 15 seconds. Both figures are picked by label and format from any footer row (`src/session_view/copilot_footer.py`: the rows under the input box, and for credits the status block above it), so a release that moves them still reads, as does a footer cell wrapped over two rows. A figure outlives a footer that shows none (a mid-repaint, a menu, a narrow cut) by one minute, then reads unknown (nothing is drawn, never a zero) and one info log line names the footer rows seen (`src/session_view/copilot_figures.py`).
- **↓ Latest** — a pill floating above the composer, shown only while you are scrolled more than one screen above the newest output; one tap jumps down. The same pill floats over Chat.
- **⌨️ Keys** — a popover D-pad above the composer with arrow, `Esc`, `Tab` and `Enter` keys for phone keyboards that lack them, plus mutually exclusive sticky toggles for Shift and Ctrl. Disabled when the session has no PTY to drive.
- **📎 Attach** — **Photo** and **Attach file (to context)** upload one or more phone files (multi-select); each is stored next to the project and its path is added to the message for review, so the agent can read it. The ordinary attach is capped at 12 MB.
- **📦 Attach large file (not read)** — for a file the agent should *have*, not *read*: a 35–100 MB archive, say. The file goes onto the machine, never into the conversation. It streams as the raw request body (`POST /api/claude-code/sessions/{sid}/large-file?name=`), relayed by the webapp to the session-host as it arrives and written to disk as it goes, so neither process holds it whole. The limit is `large_upload_max_mb` (default 2048), enforced by the webapp before anything moves and again on the streamed byte count, and passed to the session-host per upload so a change needs no session-host restart.

## Files an agent sends to Chat

Any agent in a launcher session can put a file in front of you in that session's Chat: an image or PDF shows inline, a text file shows its opening lines, a Markdown file shows its opening rendered (a tap opens the whole rendered file in the viewer), each under the agent's caption with an **Open** link. The agent posts the file to its session's outbox from the PC:

```
curl -k -X POST https://127.0.0.1:8465/api/claude-code/sessions/$APP_LAUNCHER_SESSION_ID/outbox \
  -H "Content-Type: application/json" -d '{"path": "out/chart.png", "caption": "before vs after"}'
```

`APP_LAUNCHER_SESSION_ID` is set in every launcher-spawned session, and a relative path is taken against the session's folder. The file is copied into the launcher's runtime store (`webapp/outbox/<session id>/`, gitignored; a session keeps its newest 50 and sessions untouched for 7 days are pruned), so what you see is what was sent. The path is fully resolved and must stay inside the session's own folder or one of `outbox_scratch_roots`. A `.zip`, `.gz`, `.tgz` or `.7z` is shown as a download-only card, served as an attachment and never rendered.

## How it is wired

- A separate long-lived **session-host** process (loopback only, port `8466`) owns every `copilot` ConPTY. The tray starts and owns it. Because it is its own process, restarting the webapp does not kill running sessions (a PC reboot still does).
- The webapp proxies a WebSocket from the phone to the session-host and is the single auth choke point.
- `xterm.js` renders the terminal in the SPA with no build step, vendored under `app/webapp/static/vendor/`. It and its addons and stylesheet stay out of the boot (`terminal/xterm-loader.js`). Once the session list has drawn, the scripts load right after the session views and before the other views, and the stylesheet is fetched into the cache, so the first terminal open does not wait on the download. A Terminal tap that comes before the session views have loaded fetches the stack alongside them. The stylesheet is applied only on that open, because inserting it restyles the page and WebKit then resets a scrolled list. An origin that cannot reach the terminal loads none of it. The core and its WebGL addon must be re-vendored together, since a mismatched addon throws inside `loadAddon`.
- Copilot is a full-screen differential TUI, so on (re)connect the session-host does **not** replay the raw scrollback ring (it would dump stale cursor deltas into a fresh xterm and re-answer the agent's startup terminal queries) but forces a clean repaint. `terminal_history_lines` bounds the scrollback it keeps.

## Security model — the terminal is not the same as the launcher

Launching, listing and stopping sessions stay bearer-token gated. The **live terminal itself** is stricter:

- **Private-network only.** The terminal WebSocket, image upload and WebAuthn endpoints refuse any request that arrived over a public tunnel (rejected on the `Cf-Ray` header) and require a client IP in the Tailscale CGNAT range `100.64.0.0/10`, loopback, or an entry in `tailnet_allowlist`, or, with `terminal_allow_private_lan` on, any private-network address. The setting exists for a PC with no Tailscale on a trusted home or office LAN; it is off by default, and the bearer token and passkey legs still apply after it. Settings › Terminal has the switch, with a warning before it turns on; it takes effect at once and can be changed only on the PC itself or over Tailscale, so a private-network phone cannot widen its own access. The screen view (`/api/screen/*` and its WebSocket) shows the whole desktop, so it needs a second switch, `screen_allow_private_lan`, on top: with only the first on, a private address still gets the Tailscale-only refusal on the screen.
- **The transcript is terminal-grade too.** The Chat transcript and everything folded from it (changed files, links, diffs) is the whole conversation, so it sits behind the same gate as typing, not the ordinary read gate: one rule, one answer for the whole session view.
- **Passkey-gated.** When `webauthn_rp_id` and `webauthn_origin` are set, opening or driving a terminal requires a **WebAuthn platform passkey** (Face ID, a fingerprint or the device PIN). A passkey assertion mints a short-lived (12 h) terminal token that the WebSocket and image endpoints require.
- **A device list you control.** Enrolled passkeys live in a gitignored `webauthn_devices.json` under `config/`. Enrolment only works during a 5-minute window opened deliberately from the tray (**Enroll device**), and a device is revoked from Settings.
- **Audited.** Every terminal action is logged to `webapp/terminal_audit.log` (enrol, unlock, session lifecycle, device, client IP), plus per-session `webapp/sessions/<id>.log` (input chunks, image uploads) and `<id>.transcript` (full output). The per-session files are deleted after `session_retention_days` (default 365) by a daily sweep; the cross-session audit log is not touched.
- **Keystrokes go first.** The webapp forwards each input frame to the session-host before writing its audit line, and writes that line on a one-thread writer of its own, so a keystroke never waits for a thread the file-reading polls are holding. To see where a slow echo spends its time, set `terminal_timing_log` to `true` in `config/webapp_config.json`: `webapp/slow-requests.log` then gets each keystroke's webapp stages (received→forwarded, forwarded→first output), the browser's key-to-paint for any keystroke slower than 100 ms, and every event-loop stall over 100 ms.

> A Copilot launch runs without permission prompts when `copilot_skip_permissions` is on (`--allow-all`). The marginal risk over existing private-network remote access is small, since anyone on the tailnet could already open a remote desktop session; the passkey gate and audit log make this surface more controlled than plain remote access.

### Enrolling a phone

1. On the PC, set `webauthn_rp_id` (the bare hostname the phone browses to, e.g. `pc.tailnet.ts.net`) and `webauthn_origin` (the full origin, e.g. `https://pc.tailnet.ts.net:8465`) in `config/webapp_config.json`, and restart the webapp.
2. On the phone, open the launcher over that HTTPS URL ([Android](android.md) covers certificates).
3. On the PC, tray menu → **Enroll device (5 min)**.
4. On the phone, Settings → Terminal access → **Enroll this device**, and confirm with the biometric or PIN.

After that, opening any session prompts once per 12 h.

## Terminal on the PC too

With `claude_show_local_window` on (the default), a phone launch also opens an interactive terminal window for the session on the PC. It connects over loopback, which bypasses the private-network and passkey gate, and because the session-host fans output to every connected client and accepts input from all of them, you can type from the phone and the PC interchangeably. Set the key to `false` to launch silently.
