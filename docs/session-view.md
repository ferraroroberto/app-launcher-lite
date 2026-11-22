# Session view — reference

The session view is the launcher's main interaction surface for a running Copilot session: one full-screen overlay over the Coding tab with two modes, **Terminal** and **Chat**, and a shared bar. This page is the durable reference; the README links here.

Session routes keep the full app's `/api/claude-code/...` path prefix; the name is historical and every route serves Copilot sessions here.

Contents: [Opening a session](#opening-a-session) · [The shared bar and menu](#the-shared-bar-and-menu) · [Stop and kill](#stop-and-kill) · [Terminal ⇄ Chat](#terminal--chat) · [Chat mode: reading](#chat-mode-reading) · [Changed files](#changed-files) · [Links](#links) · [Live refresh](#live-refresh) · [Loading older and newer turns](#loading-older-and-newer-turns) · [Answering a question the agent asks](#answering-a-question-the-agent-asks) · [Resuming a session](#resuming-a-session) · [Sending from Chat](#sending-from-chat) · [Detached sessions](#detached-sessions) · [Rename](#rename) · [Reader and its limits](#reader-and-its-limits) · [Not ported yet](#not-ported-yet)

## Opening a session

The Coding tab's Sessions card lists running sessions on the shared two-line row: an avatar holding the agent's icon with its alive badge, the title, and `folder · model · status · age · up uptime`. The model is the short name the session runs now (absent when unknown); the status word and last-activity age are the Board's own. Chips appear only for exceptions: the Board's `needs you` or `stalled`.

The row's ⋮ menu offers **⌨ Terminal** (full-control rows only), **💬 Chat**, **✏️ Rename** and **✕ Stop and kill**. Tapping the menu never opens the session. **Tapping the row** opens the view in the mode it was last viewed in:

- A session never opened before starts in Chat, or in Terminal when **Settings › Sessions › Open in Chat** is off (`open_sessions_in_chat`, on by default). On a desktop browser, Terminal is in the page, or in the PC window with **Open in a new window** on (`open_sessions_in_window`, off by default); the row's ⋮ menu has **Open in new window** on demand, desktop browser only.
- A detached row opens in Chat.
- The Board's drill-down drawer offers the same four actions.

## The shared bar and menu

The bar carries ‹ back, the title, an icon-only **Terminal ⇄ Chat** toggle and the **⋮** menu:

- **Rename · Copy link · Stop and kill** — always.
- **Hide steps** (or **Show steps**), **Hide automatic** (or **Show automatic**), **Load new** and **Reload** — while Chat shows.
- **Changed files** and **Links** — in either mode.

The phone's floating tab bar hides while the session view is open.

## Stop and kill

Stop asks Copilot to quit with its own command (`/exit`) so its shutdown hooks run, waits briefly for a clean exit, then force-terminates. The window always closes; there is no confirm (the chief, once active, is the exception). The command is typed as three separate keystrokes with a beat between them — Esc, the command, then Enter — because a burst lets a TUI that binds Alt-keys read Esc plus `/` as one meta key and swallow the slash, or race the harness's slash-command popup.

Every stop, resume and hand-off answers the tap at once: a pending toast shows before the request is sent, and a repeat tap on the same target sends nothing while the first is in flight. The toast is replaced by the outcome — *done*, *failed* with the server's reason, or *not confirmed* when the request outlives 20 seconds or the connection dies with no answer. A timeout never reads as success.

## Terminal ⇄ Chat

Switching modes never reconnects or reloads anything: the live terminal keeps its socket and scrollback while Chat is up, and Chat keeps its loaded pages while the terminal is up. A segment the session cannot offer is greyed and a tap says why (*Detached session — no terminal*). For the terminal itself see [Interactive terminal](interactive-terminal.md).

## Chat mode: reading

Chat shows the session's whole conversation as a chat-style list, read from Copilot's `events.jsonl` (`src/transcript_flavors/copilot.py`):

- **Your prompt** is a right-aligned bubble with its time, a copy button and a fold chevron.
- **The agent's turn** is one block: everything it does until your next prompt sits under a single header with the agent's icon and name, the time, copy and a collapse chevron. URLs in text are clickable.
- **Steps** — tool calls with their results, thinking and harness plumbing are folded per run into one quiet **step line**: the run's newest step in plain words, then muted counts (*Edited 3 files, ran 2 commands, read main.js*), the edits' total `+N −M`, a `N failed` chip and the run's duration. Opened, each item hangs off a thin rail: a command as a `$` block with its output, an edit as its diff, anything else as a card. Everything is drawn as text, never markup. **Hide steps** in the ⋮ menu leaves a plain you-and-agent exchange.
- **Hide automatic** folds turns nobody typed (the chief's doorbell line, once the chief is active) into a slim *N automatic turns ›* divider. The server marks such a prompt; the client never decides.
- **Edited N files** — a turn that edited files ends with a card listing each file with an A / M / D badge and its `+N −M` (first 3 shown, then **Show N more**). It appears once the turn is over, counts only successful steps and opens [Changed files](#changed-files) on **This turn**.
- **File chips** — a reply naming a file this session edited, when unambiguous, shows an accent chip that opens that file's diff.
- **Edit steps open as their diff**: added lines green, removed red. Copilot's recorded edit text carries no file position, so its diffs show no line-number gutter and separate hunks with `⋯`. A page carries at most 80 lines of each step's diff; **Show full diff** fetches the rest, capped at 200 KB.
- **Failed tool calls** are marked with a red glyph and a `failed` chip.

## Changed files

The ⋮ menu's **Changed files** lists every file the session's edits created, modified or deleted, in the same panel as the Coding tab's Show changes:

- Each file carries an **A** (created), **M** or **D** badge and its `+N −M`; tapping it shows the session's edits to it in order, drawn like Chat steps. A failed step counts nowhere.
- It is **built from the transcript**, so it still works once the work is committed or a worktree is removed. **D** is mostly best effort: shell deletions record no diff, so it usually means *edited and no longer on disk*.
- **Files a shell command wrote** (`Set-Content`, `echo >`, a script, a formatter) leave no edit step, so the newest window also reads `git status` of the session's folder and adds each file the transcript did not name, marked **from git status**. Only files modified since the session's first transcript timestamp count, so work already in the tree before the session is not attributed to it; a file the shell deleted cannot be dated and is left out. Its row shows git's difference against `HEAD` (which includes any earlier uncommitted work in that same file), not edit steps, and the summary line says what was read: transcript and git status, or transcript only because the folder is not a git repository, git did not answer, or the transcript has no start time. Work already committed leaves git status, so only the transcript still shows it. A resumed session keeps its original start.
- It reads one bounded window of the transcript (the newest part on open); **Load older** reads the next window and merges it.
- Copilot's `edit` and `create` tools are recognised; its `apply_patch` tool is not yet, so such edits are missing from the list.
- **Diff | File** — an opened file starts on its diff; **File** shows it as it is on disk now (JSON pretty-printed, code with line numbers and light token colours, anything else as text). **Preview** renders HTML (empty-sandbox frame, no script) and Markdown (escape-first renderer). The absolute path shows under the switch with **Copy path**.
- **Open on PC** shows only when the page runs on the PC itself (never on the phone) and opens the file in VS Code, or selects it in Explorer; it never opens a file's default app, which would run a changed `.bat` or `.ps1`.
- Opened from a turn's Edited card, the panel starts on **This turn**; **Whole session** switches to the full list.

## Links

The **Links** sheet lists the URLs in your prompts and the agent's replies, grouped **GitLab**, **Servers & previews** (localhost, LAN, tailnet hosts) and **Other**, each group folded to its name and count, newest first. Only what was *said* is listed: a URL inside a tool call, result or command output is not. A link mentioned ten times is one row.

- Each link is looked up server-side when the sheet opens for a real title: a GitLab issue or merge request through `glab` (unverified live), a local or tailnet server by its page `<title>`. A lookup gives up after 3 s, the request waits at most 4 s and runs at most 40 lookups; results are cached per URL in memory. A link with no title keeps its URL-derived label.
- Nothing credential-shaped is shown: a URL with `user:password@` is skipped and a key-like query parameter (`token`, `key`, `sig`, `auth`, `code`) or a long opaque value is dropped.
- It is folded from the transcript, reads one bounded window per request (**Read more** for older), and survives the session ending.

## Live refresh

The conversation keeps itself up to date while you read, with no pull-to-refresh:

- **Only for the chat you are looking at.** A window showing Terminal fetches no chat, a closed view fetches nothing, and a backgrounded tab does one catch-up fetch when you return.
- **Incremental.** Only what the agent appended is fetched and added at the bottom, so scroll position and open tool-call groups are undisturbed. An unchanged session costs one file-size check.
- **A session that ends** stops refreshing and says so.
- **The turn in progress** shows as one line in the bottom strip (`⏱ 15:02 · 10 actions · Running Bash`): elapsed time since your last message, the tool calls made, the newest step. It rides the refresh read, so it costs no request.

**Reload** restarts refresh after repeated network failures and re-reads from scratch; **Load new** forces the live read now and says *No new messages* when there are none. On the phone, a pull up past the bottom is Load new and a pull down at the top loads older turns.

## Loading older and newer turns

The newest turns load first in bounded pages; **Load older** (or scrolling to the top) pulls in the previous page. One tap always surfaces an older prompt or reply: a huge line is charged only 64 KB against the page's 2 MiB budget (capped at 16 MiB of real reads per request), the phone keeps fetching up to 8 pages or 2.5 s until a turn arrives, and when the file start holds only tool calls the list ends on *Start of transcript — no older messages*.

## Answering a question the agent asks

Copilot's ask tool is `ask_user`. Chat's one-tap question card and typed-answer path were built for the full app's agent; whether they drive `ask_user`, and whether `ask_user` blocks under autopilot, are **unknown** until probed (see [Not ported yet](#not-ported-yet)). Until then answer in the Terminal.

## Resuming a session

A launch with **Resume** on opens Copilot's own `--resume` picker over the PTY in Terminal (or in the detached console on the PC); the launcher never builds a session list of its own. A non-interactive run (`copilot -p`) resumed with `--session-id` keeps its context (verified on CLI 1.0.105).

## Sending from Chat

The same composer as the terminal — a tall predictive textarea plus a button grid — is docked under the chat. **➤ Send** posts to the session's input route and the sent turn appears through the live refresh. Ctrl+Enter (⌘+Enter on a Mac) sends on a desktop keyboard; plain Enter inserts a newline.

### What the toast says

| Toast | Meaning |
| --- | --- |
| *Sent* | The session-host confirmed the submit, or Enter went in with nothing to verify it. For the second, Chat then looks for up to 15 s for a new turn carrying the text, or the agent going busy. |
| *Sent, but the agent hasn't picked it up* (red) | That look found neither; the text goes back into the box. |
| *Sent, not confirmed: typed into the PC console* | A detached session has no transcript to check. |
| *Queued* | The agent was busy; Enter goes in once it settles. |
| *Not submitted* (red) | The text reached the agent but Enter never did. |
| *Send failed* | The terminal never echoed it, the console did not take it, or the session exited. The text is kept to retry. |

### Queued messages

A message sent while the agent works waits in the agent's input box until the session-host presses Enter. A **N queued** chip above the composer counts those messages. **Discard** is offered only for an agent whose queue was probed (`queue_edit` in `src/agents.py`); Copilot is not, so its queued messages are listed as *can't be removed from here*. Sending text identical to a message still pending asks first (*Already queued*).

### Keyboard, attachments and keys

- The **image button** stores a file next to the project and adds its path to the message for review; it works for a detached session too.
- The **⌨ keys** drive a full-control session's terminal even while Chat shows, once Terminal has been shown for that session. A detached session has no terminal, so they are greyed.

## Detached sessions

A detached session of the PC reads exactly the same from the phone, because Chat never needs the launcher's terminal capture. **Send** types the text into the session's console window on the PC and presses Enter. Delivery is unconfirmed by design: a detached console has no output stream to read, so the API reports `delivered: "unconfirmed"` and the toast says *Sent, not confirmed*. A tap on a greyed Send says why, and a stale session-host answers *session-host restart needed* with the running build's sha. A newline becomes a soft line break in the composer; the submitting Enter goes in separately after a short settle.

## Rename

The ⋮ menu's pencil opens **Rename**. A Board session whose issue title is known before launch is named at spawn through Copilot's `--name` flag, which syncs its native resume picker. Remote access is not a launch flag: with Settings > GitHub Copilot > **Remote session** on (the default) Lite sets the two Copilot user settings that share each new session before it starts ([docs/config.md](config.md)), and `/remote show` typed in a session reports it as shared through a Mission Control environment. Probed on CLI 1.0.114: `/remote on` typed in a session is refused with *Incoming account connections are disabled* while those settings are off, `/remote show` prints an environment id and not a link, and the CLI records no per-session remote state a card could read (`session.start` carries `remoteSteerable:false` on a shared session). A card therefore shows no remote chip; Settings shows whether Copilot's own setting is on.

## Reader and its limits

| Agent | Reads | Tool failures recorded |
| --- | --- | --- |
| GitHub Copilot CLI | `events.jsonl` | some — a tool that could not run is recorded; a shell command that merely exits non-zero is not |

An opened card whose call is unmarked says the outcome was never recorded. A session with no history file and one whose file cannot be read say so in different words (*No transcript found* versus *Couldn't read the transcript*).

## Not ported yet

These are **unknown** or planned, not working: a queued `/compact` running at turn end, a remote-control link captured and copied from the launcher, plan approval and question cards driving Copilot's `ask_user`, and a writer for the Board's `.proof/` reports (the Chat view's context ring is shipped). The `unknown` items were last probed on 1.0.105; the installed CLI is 1.0.114, on which the doorbell's composer keys are verified ([docs/board.md](board.md#the-chief)). Verified on CLI 1.0.105: interactive and `-p` sessions fire the four hooks (`sessionStart`, `userPromptSubmitted`, `agentStop`, `sessionEnd`), and `events.jsonl`'s `session.usage_record` carries `inputTokens` and `maxPromptTokens`.
