<!-- Generated from docs/ux-surfaces.yaml by scripts/ux_surface_map.py. Do not edit: change the map and re-run. -->

# UX surface map: App Launcher Lite

Every surface a person can reach in this app, each under a stable id that its root element carries as `data-surface`. Each area has a diagram of how its surfaces connect; a rounded node is a surface in another area.

One single-page app: five tabs (Board, Code, Team, Apps, Jobs) in the vendored nav, Settings behind the gear in every tab's header. Phone first; at 1100px and wider with a fine pointer the nav is a left rail and the Code tab is a list beside the session view. Every sheet is a native dialog with a header close and one full-width primary action. The coding agent is GitHub Copilot CLI and the forge is GitLab, so the Board lists GitLab issues and merge requests and the Code tab launches Copilot. Features that need a folder or a registry (Team OS skills, mini apps, the chief) show their empty state until Settings points at one. Ids are `<area>.<surface>`; the e2e capture hook takes the same ids.

Walked 2026-11-18 against build `52a7d40`, at widths 390, 1280, themes light, dark.

## Areas

| Area | Surfaces |
|---|---|
| [Chrome](#chrome) | 9 |
| [Code](#code) | 11 |
| [Terminal](#terminal) | 8 |
| [Chat](#chat) | 6 |
| [Screen](#screen) | 4 |
| [Apps](#apps) | 12 |
| [Jobs](#jobs) | 10 |
| [Team OS](#team-os) | 7 |
| [Board](#board) | 15 |
| [Settings](#settings) | 14 |
| [Keys and gestures](#keys-and-gestures) | 4 |

## Chrome

What every tab shares - the nav, each tab's header, the toast and the dialogs any tab can raise.

```mermaid
flowchart LR
  s0["Primary navigation"]
  s1["Tab header"]
  s2["PC load sheet"]
  s3["Toast"]
  s4["New version banner"]
  s5["Sign-in sheet"]
  s6["Confirm dialog"]
  s7["No session open"]
  s8["Build readout"]
  s67(["Board tab · Board"])
  s9(["Code tab · Code"])
  s60(["Team OS tab · Team OS"])
  s38(["Apps tab · Apps"])
  s50(["Jobs tab · Jobs"])
  s82(["Settings · Settings"])
  s34(["Screen view · Screen"])
  s11(["Session menu · Code"])
  s22(["Overlay session menu · Terminal"])
  s40(["Running app menu · Apps"])
  s43(["Listener menu · Apps"])
  s56(["Job menu · Jobs"])
  s65(["Skill file · Team OS"])
  s78(["Drawer menu · Board"])
  s0 -->|"Board"| s67
  s0 -->|"Code"| s9
  s0 -->|"Team"| s60
  s0 -->|"Apps"| s38
  s0 -->|"Jobs"| s50
  s1 -->|"CPU reading"| s2
  s1 -->|"gear"| s82
  s2 -->|"Screen View live"| s34
  s9 -->|"header"| s1
  s11 -->|"Stop"| s6
  s22 -->|"Stop and kill"| s6
  s40 -->|"Stop"| s6
  s43 -->|"Stop process"| s6
  s56 -->|"Remove"| s6
  s65 -->|"Delete log"| s6
  s78 -->|"Stop"| s6
```

### `chrome.nav` Primary navigation

- **Root:** `data-surface`
- **Reach:** always on screen
- **Holds:**
  - five tabs in order Board, Code, Team, Apps, Jobs, each an icon and a label
  - a count badge on the Board tab while something waits on the owner
- **Does:**
  - switches the pane and keeps the choice in the page state
  - a tab switch leaves Settings and, in the wide layout, closes the docked session view when Code is left
- **Phone:** a floating pill at the bottom edge
- **Desktop:** a left rail 80px wide
- **Why:** the fleet navigation contract; Settings is no tab, the header gear opens it
- **Leads to:**
  - [Board tab](#boardtab-board-tab) (Board)
  - [Code tab](#codetab-code-tab) (Code)
  - [Team OS tab](#teamtab-team-os-tab) (Team)
  - [Apps tab](#appstab-apps-tab) (Apps)
  - [Jobs tab](#jobstab-jobs-tab) (Jobs)

### `chrome.home-head` Tab header

- **Root:** none — the same vendored home-head markup sits at the top of all six panes, so no single id can name it.
- **Reach:** the top of every tab and of Settings
- **Holds:**
  - the tab's icon and title
  - one status line that names only exceptions, worst first, and falls back to a plain count
  - the CPU reading button, a muted percentage that takes a tone at 70 and 90 percent and shows a dash when the server could not read it
  - the theme toggle
  - the Settings gear
- **Does:**
  - the CPU reading opens the PC load sheet
  - the theme toggle flips light and dark and keeps the choice
  - the gear opens Settings
- **States:** the gear is the only way into Settings; Settings itself shows no gear
- **Leads to:**
  - [PC load sheet](#chromecpu-sheet-pc-load-sheet) (CPU reading)
  - [Settings](#settingspane-settings) (gear)

### `chrome.cpu-sheet` PC load sheet

- **Root:** `data-surface`
- **Reach:** the CPU reading in any tab header
- **Holds:**
  - the load reading and what is using it
  - a Screen row with a View live action
  - Done
- **Does:** View live opens the Screen view
- **Leads to:** [Screen view](#screenview-screen-view) (Screen View live)

### `chrome.toast` Toast

- **Root:** `data-surface`
- **Reach:** shown by an action's result, a copy, a long-press hint or a context ring tap
- **Holds:** one status line that dismisses itself
- **States:** lives on the body, outside the app wrapper, so the nav cannot clip it

### `app.update-banner` New version banner

- **Root:** `data-surface`
- **Reach:** appears by itself when the server reports a build other than the one the page booted on
- **Holds:**
  - the text New version available
  - Reload
  - a dismiss button
- **Does:**
  - Reload reloads the page; it is only ever the user's tap
  - dismiss keeps it down until the server moves to yet another build
- **States:** a server back on the boot build hides it
- **Why:** the vendored build-watch component stamps this id itself, and every fleet app lists it

### `shared.login` Sign-in sheet

- **Root:** none — the vendored sign-in component builds the sheet itself and this copy does not stamp an id.
- **Reach:** raised by any request the server answers with 401
- **Holds:** a password field and the sign-in action
- **Does:** a successful sign-in stores the token and boots the page again
- **States:** only one sheet is open however many requests fail at once

### `chrome.confirm-dialog` Confirm dialog

- **Root:** `data-surface`
- **Reach:** stopping a session or a running app, removing a job, running a job that asks for confirmation, and other destructive or costly actions
- **Holds:**
  - a title and a message
  - an optional typed-confirmation field
  - the Confirm action
- **Does:** Confirm resolves the action; the header close or Escape cancels it
- **Why:** one themed dialog stands in for the browser's confirm so a phone shows the same sheet everywhere

### `chrome.detail-empty` No session open

- **Root:** `data-surface`
- **Reach:** the Code tab in the wide layout while no session is open
- **Holds:** No session open and the line A session opens here, beside the list
- **States:** hidden below 1100px and on a coarse pointer
- **Desktop:** the detail pane's resting state

### `chrome.build-readout` Build readout

- **Root:** `data-surface`
- **Reach:** the foot of every pane
- **Holds:** the build identity of the running server
- **Why:** the vendored page-foot, so a phone shows which build it is looking at

## Code

The default tab. The session list and the project list; a session opens in the terminal overlay.

```mermaid
flowchart LR
  s9["Code tab"]
  s10["Sessions"]
  s11["Session menu"]
  s12["Stopped chief menu"]
  s13["Projects"]
  s14["Launch preset menu"]
  s15["Project menu"]
  s16["Sync all sheet"]
  s17["History sheet"]
  s18["Changes overlay"]
  s19["Changed file menu"]
  s0(["Primary navigation · Chrome"])
  s1(["Tab header · Chrome"])
  s20(["Session overlay · Terminal"])
  s25(["Links sheet · Terminal"])
  s24(["Rename session · Terminal"])
  s6(["Confirm dialog · Chrome"])
  s86(["Chief sheet · Settings"])
  s92(["Folders sheet · Settings"])
  s22(["Overlay session menu · Terminal"])
  s30(["Turn cards and step lines · Chat"])
  s0 -->|"Code"| s9
  s9 -->|"Sessions"| s10
  s9 -->|"Projects"| s13
  s9 -->|"header"| s1
  s10 -->|"row tap"| s20
  s10 -->|"row kebab"| s11
  s10 -->|"stopped chief kebab"| s12
  s11 -->|"Links"| s25
  s11 -->|"Rename"| s24
  s11 -->|"Stop"| s6
  s11 -->|"Chief settings"| s86
  s12 -->|"Chief settings"| s86
  s13 -->|"row kebab"| s15
  s13 -->|"preset dropdown"| s14
  s13 -->|"Sync all"| s16
  s13 -->|"History"| s17
  s13 -->|"Open Settings"| s92
  s13 -->|"row tap"| s20
  s15 -->|"Show changes"| s18
  s18 -->|"file kebab"| s19
  s22 -->|"Changed files"| s18
  s30 -->|"file row"| s18
```

### `code.tab` Code tab

- **Root:** `data-surface`
- **Reach:**
  - the Code tab
  - the page's default tab
- **Holds:**
  - the tab header
  - the Sessions card
  - the Projects card
- **Desktop:** the list sits left of the docked session view
- **Leads to:**
  - [Sessions](#codesessions-sessions) (Sessions)
  - [Projects](#codeprojects-projects) (Projects)
  - [Tab header](#chromehome-head-tab-header) (header)

### `code.sessions` Sessions

- **Root:** `data-surface`
- **Reach:** the first card on the Code tab, open by default
- **Holds:**
  - the chief's row first, running or stopped
  - one row per session this launcher started, with an avatar, a one-line title and a status line
  - a kebab on every row
- **Does:**
  - a row's tap opens the session in the terminal overlay, or in Chat when Open in Chat is on
  - a stopped chief row offers Start and Resume
- **States:** empty - No sessions launched from here yet, with a Show projects action that opens the Projects card
- **Leads to:**
  - [Session overlay](#terminaloverlay-session-overlay) (row tap)
  - [Session menu](#codesession-menu-session-menu) (row kebab)
  - [Stopped chief menu](#codechief-menu-stopped-chief-menu) (stopped chief kebab)

### `code.session-menu` Session menu

- **Root:** `data-surface`
- **Reach:** a session row's kebab
- **Holds:**
  - Terminal and Chat
  - Links
  - Open in new window
  - Copy path
  - Rename
  - Stop, in the danger tone
  - for the running chief, Restart and Chief settings
- **Does:**
  - Terminal and Chat open the overlay in that mode
  - Stop asks for confirmation, then kills the session
- **Leads to:**
  - [Links sheet](#terminallinks-sheet-links-sheet) (Links)
  - [Rename session](#terminalrename-dialog-rename-session) (Rename)
  - [Confirm dialog](#chromeconfirm-dialog-confirm-dialog) (Stop)
  - [Chief sheet](#settingschief-chief-sheet) (Chief settings)

### `code.chief-menu` Stopped chief menu

- **Root:** `data-surface`
- **Reach:** the kebab on the chief's row while no chief runs
- **Holds:** Chief settings; Restart is hidden while nothing runs
- **Leads to:** [Chief sheet](#settingschief-chief-sheet) (Chief settings)

### `code.projects` Projects

- **Root:** `data-surface`
- **Reach:** the second card on the Code tab, closed by default
- **Holds:**
  - the launch toolbar, with a launch preset dropdown when a preset is saved
  - History, Sync all and Refresh for the git check
  - a filter box with a favorites toggle
  - one row per project folder, with its git flag
  - the time of the last git check
- **Does:**
  - a project row launches the coding agent in that folder
  - a preset picked in the toolbar launches it on that preset's model, effort and context
  - Refresh fetches every remote and re-reads branch, changes and ahead or behind
  - Sync all fast-forwards every clean project that is only behind
  - History opens the History sheet
- **States:**
  - empty - No projects found, with an Open Settings action that opens the Folders sheet
  - filtered to nothing - No projects match the filter, with Clear filter
  - the favorites toggle narrows the list to starred projects
- **Leads to:**
  - [Project menu](#codeproject-menu-project-menu) (row kebab)
  - [Launch preset menu](#codepreset-menu-launch-preset-menu) (preset dropdown)
  - [Sync all sheet](#codegit-sync-sheet-sync-all-sheet) (Sync all)
  - [History sheet](#codehistory-sheet-history-sheet) (History)
  - [Folders sheet](#settingsfolders-folders-sheet) (Open Settings)
  - [Session overlay](#terminaloverlay-session-overlay) (row tap)

### `code.preset-menu` Launch preset menu

- **Root:** `data-surface`
- **Reach:** the preset dropdown first on the Projects launch toolbar, drawn only once a preset is saved for a model on the Copilot list
- **Holds:** Default and one row per saved preset, each named with its effort and context tier
- **Does:**
  - picking a row sets the model, effort and context the next launch sends, and is remembered on this device
  - Default sends nothing extra, so the saved Copilot settings apply

### `code.project-menu` Project menu

- **Root:** `data-surface`
- **Reach:** a project row's kebab
- **Holds:** the issues link, Open in VS Code, Show changes, Pull and Open folder
- **Does:**
  - items the project cannot do are greyed with a hint
  - Show changes opens the working-tree overlay
- **Leads to:** [Changes overlay](#codechanges-overlay-changes-overlay) (Show changes)

### `code.git-sync-sheet` Sync all sheet

- **Root:** `data-surface`
- **Reach:** Sync all in the Projects card
- **Holds:**
  - the result of the run, project by project
  - Done
- **Does:** Done closes the sheet

### `code.history-sheet` History sheet

- **Root:** `data-surface`
- **Reach:** History in the Projects card toolbar
- **Holds:**
  - a day, week or month grouping and a range of 30 days, 90 days, 6 months or a custom pair of days
  - tiles for lines added and removed, merge requests merged and issues opened and closed, each with a chart
  - a per-project table sortable by column
  - the data sources, each with its age, and the projects skipped for having no GitLab remote
  - Refresh and Done
- **Does:**
  - opens on the saved figures at once, however old, and makes no git or GitLab call
  - Refresh re-reads local git and GitLab for every project, with a toast while it runs, then updates in place
  - a column header sorts the project table
  - Done or the header close closes the sheet
- **States:**
  - loading - Reading the history, until the first answer
  - error - Could not read the history, with Retry, when no figure has loaded yet
  - stale - the last figures stay under a stale line when a later read fails
  - a source that was never read shows unknown, one whose last read failed shows stale, a project with no GitLab remote shows skipped; none of them is drawn as zero
  - a GitLab read that filled a page of 100 says older days may be incomplete

### `code.changes-overlay` Changes overlay

- **Root:** `data-surface`
- **Reach:**
  - Show changes in the project menu
  - Changed files in a session menu
  - a file chip or the Edited N files card in Chat
- **Holds:**
  - a title, a refresh button and a close
  - one collapsible row per file with a status badge, the path and plus or minus counts
  - a diff that loads the first time its row opens
- **Does:**
  - a project shows git's unified diff, tinted per line; a session shows each of its own edits in order, folded from the transcript
  - a session also lists files only git status shows it changed (a command wrote them), marked from git status, with git's diff
  - nothing here stages, commits or discards
- **States:**
  - empty - nothing changed
  - loading and error lines in a status strip
- **Leads to:** [Changed file menu](#codechanges-file-menu-changed-file-menu) (file kebab)

### `code.changes-file-menu` Changed file menu

- **Root:** `data-surface`
- **Reach:** a file row's kebab in the changes overlay
- **Holds:** Copy content, Copy diff, Copy path, Open on PC

## Terminal

The session overlay in its terminal mode - the live PTY, the composer under it, the menus on the bar.

```mermaid
flowchart LR
  s20["Session overlay"]
  s21["Terminal"]
  s22["Overlay session menu"]
  s23["Switch model sheet"]
  s24["Rename session"]
  s25["Links sheet"]
  s26["On-screen keys"]
  s27["Attach menu"]
  s10(["Sessions · Code"])
  s11(["Session menu · Code"])
  s13(["Projects · Code"])
  s28(["Chat pane · Chat"])
  s18(["Changes overlay · Code"])
  s6(["Confirm dialog · Chrome"])
  s77(["Session drawer · Board"])
  s99(["Composer keys · Keys and gestures"])
  s10 -->|"row tap"| s20
  s11 -->|"Links"| s25
  s11 -->|"Rename"| s24
  s13 -->|"row tap"| s20
  s20 -->|"Terminal mode"| s21
  s20 -->|"model chip"| s23
  s20 -->|"Chat mode"| s28
  s20 -->|"bar kebab"| s22
  s21 -->|"keys button"| s26
  s21 -->|"attach button"| s27
  s22 -->|"Rename"| s24
  s22 -->|"Links"| s25
  s22 -->|"Changed files"| s18
  s22 -->|"Stop and kill"| s6
  s77 -->|"Terminal"| s20
  s99 -->|"keys button"| s26
```

### `terminal.overlay` Session overlay

- **Root:** `data-surface`
- **Reach:** a session row, a project launch, the Board drawer's Terminal or Chat, a ?session= link
- **Holds:**
  - a bar with Back, the session title, the model chip, the context ring, the Terminal and Chat toggle and the session menu
  - the terminal host and its composer
  - a status strip
- **Does:**
  - Back closes the overlay
  - the toggle swaps Terminal and Chat without reconnecting
- **States:**
  - a session with no transcript reader offers no Chat
  - the model chip hides when the model is not known
  - the context ring hides when the agent's screen shows no context figure, as on a Copilot session too narrow for its status line
- **Phone:** covers the page
- **Desktop:** docks beside the session list in the wide layout
- **Leads to:**
  - [Terminal](#terminalhost-terminal) (Terminal mode)
  - [Switch model sheet](#terminalmodel-sheet-switch-model-sheet) (model chip)
  - [Chat pane](#chatoverlay-chat-pane) (Chat mode)
  - [Overlay session menu](#terminalsession-menu-overlay-session-menu) (bar kebab)

### `terminal.host` Terminal

- **Root:** `data-surface`
- **Reach:** the Terminal side of the overlay toggle
- **Holds:**
  - the xterm screen
  - a Jump to latest button once scrolled back
  - the composer bar
  - a status strip and an activity line
- **Does:** the page connects the PTY lazily and a reconnect redraws from the kept history
- **Leads to:**
  - [On-screen keys](#terminalkeys-popover-on-screen-keys) (keys button)
  - [Attach menu](#terminalattach-menu-attach-menu) (attach button)

### `terminal.session-menu` Overlay session menu

- **Root:** `data-surface`
- **Reach:** the kebab on the overlay bar
- **Holds:**
  - Rename, Copy link, Changed files, Links
  - in Chat mode Show or Hide steps, Show or Hide automatic, Load new and Reload
  - Stop and kill
- **Leads to:**
  - [Rename session](#terminalrename-dialog-rename-session) (Rename)
  - [Links sheet](#terminallinks-sheet-links-sheet) (Links)
  - [Changes overlay](#codechanges-overlay-changes-overlay) (Changed files)
  - [Confirm dialog](#chromeconfirm-dialog-confirm-dialog) (Stop and kill)

### `terminal.model-sheet` Switch model sheet

- **Root:** `data-surface`
- **Reach:** the model chip in the session view's bar, shown for a Copilot session
- **Holds:**
  - a note saying the switch is for this session only and the Settings default stays
  - one row per model on the Copilot list, the current one checked
- **Does:**
  - a pick types Copilot's own /model for this session and waits for its event log to report the change
  - Done closes
- **States:**
  - a switch the server could not confirm leaves the old model showing and says why
  - a busy Copilot, unsent prompt text or an open menu refuses the switch before anything is typed

### `terminal.rename-dialog` Rename session

- **Root:** `data-surface`
- **Reach:** Rename in either session menu
- **Holds:**
  - a field whose blank value means the automatic title
  - Save

### `terminal.links-sheet` Links sheet

- **Root:** `data-surface`
- **Reach:** Links in either session menu
- **Holds:**
  - the artifacts and URLs the transcript mentioned, in groups Artifacts, GitHub, Servers and previews, Other
  - each group folded to its name and count, newest row first
  - a note on how far back the read reached with Read more
  - Done
- **Does:**
  - a row opens the link in a new tab and its copy button copies the URL
  - credential-looking query strings were already removed by the server

### `terminal.keys-popover` On-screen keys

- **Root:** `data-surface`
- **Reach:** the keys button in the composer
- **Holds:**
  - arrow keys, Esc, Tab and Enter
  - sticky Shift and Ctrl, only one engaged at a time
- **Does:**
  - each key writes its escape sequence down the PTY input channel
  - the popover stays open across arrow and Tab taps
- **Why:** phone keyboards that lack these keys still need an agent's prompts to be navigable

### `terminal.attach-menu` Attach menu

- **Root:** `data-surface`
- **Reach:** the attach button on a composer
- **Holds:** Photo, Extract text (OCR), Attach file (to context) and Attach large file (not read)
- **Does:**
  - an upload stores the file on the PC and puts its path in the message
  - several picks queue behind each other and show N of M
- **Why:** the composer is shared by the terminal, Chat, the Board drawer and the answer sheet

## Chat

The session overlay in its chat mode - the same session read from the agent's own history.

```mermaid
flowchart LR
  s28["Chat pane"]
  s29["Question and plan cards"]
  s30["Turn cards and step lines"]
  s31["Queued messages"]
  s32["Image lightbox"]
  s33["HTML viewer"]
  s20(["Session overlay · Terminal"])
  s18(["Changes overlay · Code"])
  s77(["Session drawer · Board"])
  s20 -->|"Chat mode"| s28
  s28 -->|"queued chip"| s31
  s28 -->|"image thumbnail"| s32
  s28 -->|"HTML document card"| s33
  s28 -->|"the agent asks"| s29
  s28 -->|"a finished turn"| s30
  s30 -->|"file row"| s18
  s77 -->|"Chat"| s28
```

### `chat.overlay` Chat pane

- **Root:** `data-surface`
- **Reach:**
  - the Chat side of the overlay toggle
  - Open in Chat
- **Holds:**
  - the transcript, newest page first, with Load older
  - a Latest button once scrolled up
  - the queued-messages chip
  - the composer
- **Does:** the agent's replies and the user's prompts show expanded and everything else folds into one step line per run
- **States:**
  - empty, loading and error lines in the transcript state strip
  - a detached session has no terminal, so a note says delivery is typed into the PC console and not confirmed
- **Leads to:**
  - [Queued messages](#chatqueued-sheet-queued-messages) (queued chip)
  - [Image lightbox](#chatlightbox-image-lightbox) (image thumbnail)
  - [HTML viewer](#chathtml-viewer-html-viewer) (HTML document card)
  - [Question and plan cards](#chatdecision-cards-question-and-plan-cards) (the agent asks)
  - [Turn cards and step lines](#chatturn-cards-turn-cards-and-step-lines) (a finished turn)

### `chat.decision-cards` Question and plan cards

- **Root:** none — drawn inside the transcript list, which carries no id of its own for a card.
- **Reach:** while the agent asks a question or waits on a plan
- **Holds:**
  - the question card with numbered options, an Other field and Done
  - the plan card, answered from the terminal's own screen
  - the resume card, a searchable list of the project's conversations, shown while the terminal's picker is open
- **Does:** an option sends its answer into the session

### `chat.turn-cards` Turn cards and step lines

- **Root:** none — rows in the transcript list, drawn by the transcript renderer without a root of their own.
- **Reach:** every finished turn
- **Holds:**
  - the Edited N files card with a total and one row per file, showing the first few and Show N more
  - file chips in a reply
  - one step line per run of tool calls, opening item by item
  - a divider row that folds turns nobody typed
- **Does:** a file row or the card header opens the changes overlay
- **Leads to:** [Changes overlay](#codechanges-overlay-changes-overlay) (file row)

### `chat.queued-sheet` Queued messages

- **Root:** `data-surface`
- **Reach:** the queued chip in the Chat pane
- **Holds:**
  - the messages the session holds that the agent has not taken yet
  - a Discard on each
  - Done
- **States:** empty - The agent has taken everything

### `chat.lightbox` Image lightbox

- **Root:** `data-surface`
- **Reach:** a transcript thumbnail
- **Holds:** the full-size image and a close button
- **Does:** a tap anywhere dismisses it

### `chat.html-viewer` HTML viewer

- **Root:** `data-surface`
- **Reach:** a sent HTML document's card
- **Holds:** the file name, a close button, the document and a Download link
- **Does:** the document shows without scripts, in a frame with an empty sandbox
- **Why:** an HTML document never renders same-origin or scriptable

## Screen

The PC's desktop, live, behind the terminal's passkey gate.

```mermaid
flowchart LR
  s34["Screen view"]
  s35["Paste to PC sheet"]
  s36["Quality sheet"]
  s37["Readout"]
  s2(["PC load sheet · Chrome"])
  s98(["Screen view keys · Keys and gestures"])
  s2 -->|"Screen View live"| s34
  s34 -->|"Quality"| s36
  s34 -->|"Paste to PC"| s35
  s34 -->|"readout switch"| s37
  s98 -->|"Screen view"| s34
```

### `screen.view` Screen view

- **Root:** `data-surface`
- **Reach:** View live in the PC load sheet
- **Holds:**
  - the live frame of the PC's desktop
  - modes View, Control, Touch and Trackpad
  - a keyboard button, a quality button and a handle that shows or hides the controls
  - a keys bar with Esc, Tab, Ctrl, Alt, Win, Del, the F keys, Task Mgr, Paste to PC and Paste plus Ctrl+V
- **Does:**
  - the server sends a frame only when the screen changes, so a still picture is a quiet socket
  - capture runs only while a viewer is connected
- **States:** reconnecting and unavailable states
- **Phone:** opens full screen
- **Desktop:** pops out into its own window, /?screen=1
- **Leads to:**
  - [Quality sheet](#screenquality-sheet-quality-sheet) (Quality)
  - [Paste to PC sheet](#screenpaste-sheet-paste-to-pc-sheet) (Paste to PC)
  - [Readout](#screenreadout-readout) (readout switch)

### `screen.paste-sheet` Paste to PC sheet

- **Root:** `data-surface`
- **Reach:** Paste to PC when the phone does not let the page read its clipboard
- **Holds:** a text box and Send to PC

### `screen.quality-sheet` Quality sheet

- **Root:** `data-surface`
- **Reach:** the Quality button in the Screen view
- **Holds:**
  - the quality choices
  - the Readout switch
  - Done

### `screen.readout` Readout

- **Root:** none — a line drawn inside the Screen view while the Readout switch is on.
- **Reach:** the Readout switch in the quality sheet
- **Holds:** frames a second, data a second, the average frame size and the time from confirming to the picture

## Apps

```mermaid
flowchart LR
  s38["Apps tab"]
  s39["Running"]
  s40["Running app menu"]
  s41["Other ports card"]
  s42["Other ports sheet"]
  s43["Listener menu"]
  s44["Apps list"]
  s45["App menu"]
  s46["Rename"]
  s47["New entries sheet"]
  s48["Trays"]
  s49["Mini apps"]
  s0(["Primary navigation · Chrome"])
  s6(["Confirm dialog · Chrome"])
  s0 -->|"Apps"| s38
  s38 -->|"Running"| s39
  s38 -->|"Other ports"| s41
  s38 -->|"Apps"| s44
  s38 -->|"Trays"| s48
  s38 -->|"Mini apps"| s49
  s39 -->|"row kebab"| s40
  s39 -->|"Other ports"| s42
  s40 -->|"Stop"| s6
  s41 -->|"row kebab"| s43
  s42 -->|"row kebab"| s43
  s43 -->|"Stop process"| s6
  s44 -->|"row kebab"| s45
  s44 -->|"Scan for new apps"| s47
  s45 -->|"Rename"| s46
```

### `apps.tab` Apps tab

- **Root:** `data-surface`
- **Reach:** the Apps tab
- **Holds:**
  - the tab header
  - Running, Other ports, Apps, Trays and Mini apps cards
- **Leads to:**
  - [Running](#appsrunning-running) (Running)
  - [Other ports card](#appsother-ports-card-other-ports-card) (Other ports)
  - [Apps list](#appsapps-apps-list) (Apps)
  - [Trays](#appstrays-trays) (Trays)
  - [Mini apps](#appsmini-apps-mini-apps) (Mini apps)

### `apps.running` Running

- **Root:** `data-surface`
- **Reach:** the first card on the Apps tab
- **Holds:**
  - one row per app launched from here, with a kebab
  - an Other ports row that opens the sheet
  - an idle line and the note Only apps launched from here appear
- **States:**
  - empty - Nothing launched from here is running, with Show apps
  - an error line in a status strip
- **Leads to:**
  - [Running app menu](#appsrunning-menu-running-app-menu) (row kebab)
  - [Other ports sheet](#appsother-ports-sheet-other-ports-sheet) (Other ports)

### `apps.running-menu` Running app menu

- **Root:** `data-surface`
- **Reach:** a running app's kebab
- **Holds:**
  - a header line with the process id
  - Copy URL and Stop
- **Leads to:** [Confirm dialog](#chromeconfirm-dialog-confirm-dialog) (Stop)

### `apps.other-ports-card` Other ports card

- **Root:** `data-surface`
- **Reach:** the Apps tab in the wide layout
- **Holds:** the listeners on common app ports that this launcher did not start
- **Phone:** the Other ports row in Running opens the same list as a sheet
- **Desktop:** a card beside the lists
- **Leads to:** [Listener menu](#appslistener-menu-listener-menu) (row kebab)

### `apps.other-ports-sheet` Other ports sheet

- **Root:** `data-surface`
- **Reach:** the Other ports row in the Running card
- **Holds:**
  - the listeners
  - Check again
- **States:** empty - Nothing running on common app ports; the launcher itself is not listed
- **Leads to:** [Listener menu](#appslistener-menu-listener-menu) (row kebab)

### `apps.listener-menu` Listener menu

- **Root:** `data-surface`
- **Reach:** a listener row's kebab
- **Holds:** Open, Copy URL and Stop process in the danger tone
- **Leads to:** [Confirm dialog](#chromeconfirm-dialog-confirm-dialog) (Stop process)

### `apps.apps` Apps list

- **Root:** `data-surface`
- **Reach:** the Apps card
- **Holds:**
  - a filter box
  - one row per registered app, each with a kind and a kebab
  - the Scan for new apps row
- **States:**
  - empty - No apps registered yet, with Scan for apps
  - filtered to nothing - No apps match the filter, with Clear filter
  - an error line in a status strip
- **Leads to:**
  - [App menu](#appsapp-menu-app-menu) (row kebab)
  - [New entries sheet](#appsscan-sheet-new-entries-sheet) (Scan for new apps)

### `apps.app-menu` App menu

- **Root:** `data-surface`
- **Reach:** an app row's kebab
- **Holds:** Launch visible, Open link, Copy URL, Rename, Copy path
- **Leads to:** [Rename](#appsrename-dialog-rename) (Rename)

### `apps.rename-dialog` Rename

- **Root:** `data-surface`
- **Reach:** Rename in the app menu
- **Holds:** a field and Save
- **Why:** one dialog serves every rename on the Apps tab

### `apps.scan-sheet` New entries sheet

- **Root:** `data-surface`
- **Reach:** Scan for new apps, or the empty state's action
- **Holds:**
  - the entries the scan found, grouped, each selectable
  - Add selected
- **Does:** Add selected writes the chosen rows to the registry

### `apps.trays` Trays

- **Root:** `data-surface`
- **Reach:** the Trays card, closed by default
- **Holds:**
  - one row per sister tray found, with a switch for starting with the launcher
  - the note that switched trays start with App Launcher Lite
- **States:** empty - No sister trays found yet, with Scan for trays

### `apps.mini-apps` Mini apps

- **Root:** `data-surface`
- **Reach:** the Mini apps card
- **Holds:** one row per mini app with a start-on-launch switch
- **States:**
  - hidden until the server reports a mini-apps folder
  - empty - No mini apps found yet, with Scan for mini apps

## Jobs

```mermaid
flowchart LR
  s50["Jobs tab"]
  s51["Next up"]
  s52["Schedule sheet"]
  s53["This week"]
  s54["Jobs list"]
  s55["Sort and filter"]
  s56["Job menu"]
  s57["Job sheet"]
  s58["Run job"]
  s59["Add or edit job"]
  s0(["Primary navigation · Chrome"])
  s6(["Confirm dialog · Chrome"])
  s0 -->|"Jobs"| s50
  s50 -->|"Next up"| s51
  s50 -->|"Jobs"| s54
  s50 -->|"wide layout"| s53
  s51 -->|"Next up title"| s52
  s54 -->|"filters"| s55
  s54 -->|"Add job"| s59
  s54 -->|"row tap"| s57
  s54 -->|"row kebab"| s56
  s56 -->|"Run now"| s58
  s56 -->|"Edit"| s59
  s56 -->|"Remove"| s6
  s57 -->|"Run now"| s58
```

### `jobs.tab` Jobs tab

- **Root:** `data-surface`
- **Reach:** the Jobs tab
- **Holds:**
  - the tab header
  - the Next up card
  - the Jobs card
- **Desktop:** the detail pane's resting state is the This week schedule
- **Leads to:**
  - [Next up](#jobsnext-up-next-up) (Next up)
  - [Jobs list](#jobslist-jobs-list) (Jobs)
  - [This week](#jobsweek-this-week) (wide layout)

### `jobs.next-up` Next up

- **Root:** `data-surface`
- **Reach:** the top card on the Jobs tab
- **Holds:**
  - the next scheduled runs
  - a button on its title that opens the schedule
- **States:** loading, empty and error lines
- **Leads to:** [Schedule sheet](#jobsagenda-sheet-schedule-sheet) (Next up title)

### `jobs.agenda-sheet` Schedule sheet

- **Root:** `data-surface`
- **Reach:** the Next up title
- **Holds:**
  - the schedule over the coming days
  - Done

### `jobs.week` This week

- **Root:** `data-surface`
- **Reach:** the Jobs tab in the wide layout with no job open
- **Holds:** the week's runs

### `jobs.list` Jobs list

- **Root:** `data-surface`
- **Reach:** the Jobs card
- **Holds:**
  - a search box over jobs and run output, a filters button and an add button
  - one row per job with its last status, a spark of recent runs and a kebab
- **States:**
  - loading - Loading jobs
  - empty - No jobs registered yet, with Add job
  - no match - No jobs or run output match the search, with Clear search
- **Leads to:**
  - [Sort and filter](#jobssort-sheet-sort-and-filter) (filters)
  - [Add or edit job](#jobsjob-dialog-add-or-edit-job) (Add job)
  - [Job sheet](#jobsjob-sheet-job-sheet) (row tap)
  - [Job menu](#jobsjob-menu-job-menu) (row kebab)

### `jobs.sort-sheet` Sort and filter

- **Root:** `data-surface`
- **Reach:** the filters button in the jobs search
- **Holds:**
  - Sort by Next run or A to Z
  - Clear and Done

### `jobs.job-menu` Job menu

- **Root:** `data-surface`
- **Reach:** a job row's kebab
- **Holds:** Run now, Pause or Resume, Dry-run check, Edit, Remove in the danger tone
- **Leads to:**
  - [Run job](#jobsrun-dialog-run-job) (Run now)
  - [Add or edit job](#jobsjob-dialog-add-or-edit-job) (Edit)
  - [Confirm dialog](#chromeconfirm-dialog-confirm-dialog) (Remove)

### `jobs.job-sheet` Job sheet

- **Root:** `data-surface`
- **Reach:** a job row's tap
- **Holds:**
  - the job's name, schedule and latest runs with their output
  - Run now
- **Leads to:** [Run job](#jobsrun-dialog-run-job) (Run now)

### `jobs.run-dialog` Run job

- **Root:** `data-surface`
- **Reach:** Run now from the menu or the sheet
- **Holds:**
  - a field for each declared parameter
  - a Dry run switch
  - Run
- **Does:** a dry run asks scripts that support it to skip their side effects

### `jobs.job-dialog` Add or edit job

- **Root:** `data-surface`
- **Reach:** the add button, Add job in the empty state, Edit in the job menu
- **Holds:**
  - name, kind, the script file or inline body or HTTP check fields, arguments
  - a schedule with its type fields, cooldown and exclusive group
  - switch for Require confirmation
  - the run-on-success and run-on-failure chains, parameters and a webhook
  - Save anyway and Add and verify
- **Does:**
  - the fields shown follow the kind and the schedule type
  - Add and verify checks the script before saving; Save anyway skips that check
- **States:** a verification failure keeps the dialog open with the reason

## Team OS

```mermaid
flowchart LR
  s60["Team OS tab"]
  s61["Skills"]
  s62["Skill model menu"]
  s63["Skill menu"]
  s64["Skill sheet"]
  s65["Skill file"]
  s66["Weekly recap"]
  s0(["Primary navigation · Chrome"])
  s92(["Folders sheet · Settings"])
  s6(["Confirm dialog · Chrome"])
  s0 -->|"Team"| s60
  s60 -->|"Skills"| s61
  s60 -->|"Weekly recap"| s66
  s61 -->|"model picker"| s62
  s61 -->|"row kebab"| s63
  s61 -->|"Open Settings"| s92
  s63 -->|"Read"| s64
  s64 -->|"a file"| s65
  s65 -->|"Delete log"| s6
```

### `team.tab` Team OS tab

- **Root:** `data-surface`
- **Reach:** the Team tab
- **Holds:**
  - the tab header
  - the Skills card
  - the Weekly recap card
- **Leads to:**
  - [Skills](#teamskills-skills) (Skills)
  - [Weekly recap](#teamrecap-weekly-recap) (Weekly recap)

### `team.skills` Skills

- **Root:** `data-surface`
- **Reach:** the Skills card
- **Holds:**
  - a model picker for the launched skill
  - the launch toolbar
  - a filter box
  - one row per skill found under the Team OS folder, each with a kebab
- **States:**
  - empty - No team-os skills found, with an Open Settings action that opens the Folders sheet
  - filtered to nothing - No skills match the filter, with Clear filter
  - an error line in a status strip
- **Leads to:**
  - [Skill model menu](#teammodel-menu-skill-model-menu) (model picker)
  - [Skill menu](#teamskill-menu-skill-menu) (row kebab)
  - [Folders sheet](#settingsfolders-folders-sheet) (Open Settings)

### `team.model-menu` Skill model menu

- **Root:** `data-surface`
- **Reach:** the model picker above the skills
- **Holds:** Default and every model Settings offers

### `team.skill-menu` Skill menu

- **Root:** `data-surface`
- **Reach:** a skill row's kebab
- **Holds:** Start new session, Pick a session, Read
- **Leads to:** [Skill sheet](#teamskill-sheet-skill-sheet) (Read)

### `team.skill-sheet` Skill sheet

- **Root:** `data-surface`
- **Reach:** Read in the skill menu
- **Holds:**
  - the skill's files
  - Done
- **Leads to:** [Skill file](#teamdoc-sheet-skill-file) (a file)

### `team.doc-sheet` Skill file

- **Root:** `data-surface`
- **Reach:** a file in the skill sheet
- **Holds:**
  - the file's text
  - Rename log and Delete log for a log file
  - Done
- **Leads to:** [Confirm dialog](#chromeconfirm-dialog-confirm-dialog) (Delete log)

### `team.recap` Weekly recap

- **Root:** `data-surface`
- **Reach:** under the Skills card
- **Holds:** the weekly recap rows
- **States:** hidden until there is a recap

## Board

```mermaid
flowchart LR
  s67["Board tab"]
  s68["Project combo list"]
  s69["Backlog"]
  s70["Start with menu"]
  s71["Issue menu"]
  s72["Since you last looked"]
  s73["Chief's plan"]
  s74["Plan note"]
  s75["Bot's turn"]
  s76["Your turn"]
  s77["Session drawer"]
  s78["Drawer menu"]
  s79["Answer the chief"]
  s80["MRs and jobs"]
  s81["Done today"]
  s0(["Primary navigation · Chrome"])
  s20(["Session overlay · Terminal"])
  s28(["Chat pane · Chat"])
  s6(["Confirm dialog · Chrome"])
  s0 -->|"Board"| s67
  s67 -->|"project combo"| s68
  s67 -->|"Backlog"| s69
  s67 -->|"Since you last looked"| s72
  s67 -->|"Chief's plan"| s73
  s67 -->|"Bot's turn"| s75
  s67 -->|"Your turn"| s76
  s67 -->|"MRs and jobs"| s80
  s67 -->|"Done today"| s81
  s69 -->|"Start with"| s70
  s69 -->|"row kebab"| s71
  s73 -->|"a row's note"| s74
  s73 -->|"questions"| s79
  s75 -->|"row tap"| s77
  s76 -->|"session row"| s77
  s76 -->|"Answer"| s79
  s77 -->|"kebab"| s78
  s77 -->|"Terminal"| s20
  s77 -->|"Chat"| s28
  s78 -->|"Stop"| s6
```

### `board.tab` Board tab

- **Root:** `data-surface`
- **Reach:**
  - the Board tab
  - a ?board= deep link
- **Holds:**
  - the tab header
  - a project combo, Refresh and Collapse all
  - the lanes Backlog, Since you last looked, Chief's plan, Bot's turn, Your turn, MRs and jobs and Done today
- **Does:**
  - Refresh runs glab for GitLab issues, merge requests and what finished today
  - the project combo narrows every lane to one repository
- **States:** a status strip and a fetch-state strip, each hidden until there is something to say
- **Desktop:** the Board spans the whole width right of the rail
- **Leads to:**
  - [Project combo list](#boardrepo-menu-project-combo-list) (project combo)
  - [Backlog](#boardbacklog-backlog) (Backlog)
  - [Since you last looked](#boardjournal-since-you-last-looked) (Since you last looked)
  - [Chief's plan](#boardchief-plan-chiefs-plan) (Chief's plan)
  - [Bot's turn](#boardbots-turn-bots-turn) (Bot's turn)
  - [Your turn](#boardyour-turn-your-turn) (Your turn)
  - [MRs and jobs](#boardmrs-jobs-mrs-and-jobs) (MRs and jobs)
  - [Done today](#boarddone-done-today) (Done today)

### `board.repo-menu` Project combo list

- **Root:** `data-surface`
- **Reach:** typing in or focusing the project box
- **Holds:** the repositories that match, and a clear button on the box
- **States:** the box reads All projects when nothing is chosen

### `board.backlog` Backlog

- **Root:** `data-surface`
- **Reach:** the Backlog lane
- **Holds:**
  - a count and a Start with model picker
  - issue rows grouped in buckets, each with a kebab
- **Does:** Start and YOLO on a row use the picked model
- **States:** empty and error lines
- **Leads to:**
  - [Start with menu](#boardmodel-menu-start-with-menu) (Start with)
  - [Issue menu](#boardissue-menu-issue-menu) (row kebab)

### `board.model-menu` Start with menu

- **Root:** `data-surface`
- **Reach:** the Start with picker in the Backlog
- **Holds:** Default and every model Settings offers

### `board.issue-menu` Issue menu

- **Root:** `data-surface`
- **Reach:** a Backlog row's kebab
- **Holds:** YOLO (start and ship), Open on GitLab, Copy link

### `board.journal` Since you last looked

- **Root:** `data-surface`
- **Reach:** a card above Chief's plan
- **Holds:**
  - the chief's journal entries, new ones first
  - Clear
- **Does:** Clear stamps the last look on the server, so every device agrees
- **States:**
  - hidden when there is no journal file
  - a journal that cannot be read is one muted line, never a toast
  - with no new entry the card shows one line and no button

### `board.chief-plan` Chief's plan

- **Root:** `data-surface`
- **Reach:** the card under Backlog
- **Holds:**
  - the chief's lanes, its queue and what waits on the owner
  - Clear merged
  - a row's note, when it has one
- **Leads to:**
  - [Plan note](#boardplan-note-sheet-plan-note) (a row's note)
  - [Answer the chief](#boardanswer-sheet-answer-the-chief) (questions)

### `board.plan-note-sheet` Plan note

- **Root:** `data-surface`
- **Reach:** a plan row that carries a note
- **Holds:**
  - the row's title, repo and number, model and status
  - the note in full
  - Open on GitLab when the row links to an issue
- **Why:** a phone has no hover, so the note is read in a sheet

### `board.bots-turn` Bot's turn

- **Root:** `data-surface`
- **Reach:** the lane under Chief's plan
- **Holds:** live sessions that are working, one row each
- **Does:** a row opens the drawer
- **States:** empty line
- **Leads to:** [Session drawer](#boarddrawer-session-drawer) (row tap)

### `board.your-turn` Your turn

- **Root:** `data-surface`
- **Reach:** the lane under Bot's turn
- **Holds:**
  - live sessions waiting on the owner
  - a needs folder trust chip on a session sitting on Copilot's trust prompt
  - the owner-check groups Before you start, Validate to close and Try what shipped, one fold each
- **Does:**
  - a row unfolds its detail in place
  - Trust this folder on a needs-folder-trust row answers Copilot's prompt with remember this folder
  - a group's Answer opens the answer sheet
- **Leads to:**
  - [Session drawer](#boarddrawer-session-drawer) (session row)
  - [Answer the chief](#boardanswer-sheet-answer-the-chief) (Answer)

### `board.drawer` Session drawer

- **Root:** none — the drawer is the open row's own list item, kept across the poll re-render, so it has no element of its own.
- **Reach:** a live session row in Bot's turn or Your turn
- **Holds:**
  - the last exchange between the user and the agent
  - the shared composer
  - Chat and Terminal
  - a kebab with Rename and Stop
- **Does:** the chief's exchange poll and a live mic stop when the drawer closes
- **Leads to:**
  - [Drawer menu](#boarddrawer-menu-drawer-menu) (kebab)
  - [Session overlay](#terminaloverlay-session-overlay) (Terminal)
  - [Chat pane](#chatoverlay-chat-pane) (Chat)

### `board.drawer-menu` Drawer menu

- **Root:** `data-surface`
- **Reach:** the drawer's kebab
- **Holds:** Rename and Stop in the danger tone
- **Leads to:** [Confirm dialog](#chromeconfirm-dialog-confirm-dialog) (Stop)

### `board.answer-sheet` Answer the chief

- **Root:** `data-surface`
- **Reach:** Answer in an owner-check group or a chief question
- **Holds:**
  - one block per waiting item with its options, the recommended one already picked
  - an Other field on every item
  - an Anything else box
  - Done
- **Does:** Done sends every answer at once
- **Why:** one sheet answers everything that waits, instead of one dialog per question

### `board.mrs-jobs` MRs and jobs

- **Root:** `data-surface`
- **Reach:** the lane under Your turn, closed by default
- **Holds:**
  - merge requests and pipeline jobs, one row each
  - a project whose default-branch pipeline is red, one row each with a CI failed chip that opens the failed pipeline on GitLab
- **States:**
  - empty line
  - a project whose pipelines could not be read, one row with a CI unknown chip

### `board.done` Done today

- **Root:** `data-surface`
- **Reach:** the last lane, closed by default
- **Holds:**
  - what finished today
  - Show earlier, which adds one more day per tap up to a week
- **States:** empty line

## Settings

A group list in a pane; every row that holds more than a switch opens a sheet with a back arrow to the pane.

```mermaid
flowchart LR
  s82["Settings"]
  s83["Settings rows"]
  s84["Document sheet"]
  s85["GitHub Copilot sheet"]
  s86["Chief sheet"]
  s87["Chief model menu"]
  s88["GitLab sheet"]
  s89["Speech to text sheet"]
  s90["Launch preset menu"]
  s91["API tokens sheet"]
  s92["Folders sheet"]
  s93["Chat folder menu"]
  s94["Passkeys sheet"]
  s95["Terminal sheet"]
  s1(["Tab header · Chrome"])
  s11(["Session menu · Code"])
  s12(["Stopped chief menu · Code"])
  s13(["Projects · Code"])
  s61(["Skills · Team OS"])
  s1 -->|"gear"| s82
  s11 -->|"Chief settings"| s86
  s12 -->|"Chief settings"| s86
  s13 -->|"Open Settings"| s92
  s61 -->|"Open Settings"| s92
  s82 -->|"the groups"| s83
  s82 -->|"a document row"| s84
  s82 -->|"Coding agent"| s85
  s82 -->|"Chief"| s86
  s82 -->|"GitLab"| s88
  s82 -->|"Speech to text"| s89
  s82 -->|"API tokens"| s91
  s82 -->|"Folders"| s92
  s82 -->|"Passkeys"| s94
  s82 -->|"Terminal"| s95
  s85 -->|"preset kebab"| s90
  s86 -->|"model"| s87
  s92 -->|"a folder's kebab"| s93
```

### `settings.pane` Settings

- **Root:** `data-surface`
- **Reach:** the gear in any tab header
- **Holds:** Documents, Appearance, Agents, Sessions, Board, Connections and This PC groups
- **Does:** a tab switch leaves it
- **States:** the Documents group is hidden until the server lists documents; an unreadable list shows Couldn't read the documents list with Retry
- **Leads to:**
  - [Settings rows](#settingsgroups-settings-rows) (the groups)
  - [Document sheet](#settingsdocument-document-sheet) (a document row)
  - [GitHub Copilot sheet](#settingscopilot-github-copilot-sheet) (Coding agent)
  - [Chief sheet](#settingschief-chief-sheet) (Chief)
  - [GitLab sheet](#settingsgitlab-gitlab-sheet) (GitLab)
  - [Speech to text sheet](#settingsspeech-speech-to-text-sheet) (Speech to text)
  - [API tokens sheet](#settingstokens-api-tokens-sheet) (API tokens)
  - [Folders sheet](#settingsfolders-folders-sheet) (Folders)
  - [Passkeys sheet](#settingspasskeys-passkeys-sheet) (Passkeys)
  - [Terminal sheet](#settingsterminal-terminal-sheet) (Terminal)

### `settings.groups` Settings rows

- **Root:** none — rows inside the Settings pane, which has one root for all of them.
- **Reach:** the Settings pane
- **Holds:**
  - Appearance - a Text size choice of Small, Default and Large
  - Sessions - the switches Open in a new window and Open in Chat, with a help line that explains both
  - This PC - the switches Start at log on and Awake with lid closed, each with a help line
- **Does:** a switch saves when tapped and shows ON or OFF
- **States:** Awake with lid closed is hidden on a machine that cannot set it, and shows its own status line

### `settings.document` Document sheet

- **Root:** `data-surface`
- **Reach:** a row in the Documents group
- **Holds:**
  - the document's text
  - Copy Markdown and Open on GitHub
  - Retry when the read failed
  - Done

### `settings.copilot` GitHub Copilot sheet

- **Root:** `data-surface`
- **Reach:** Coding agent in Agents
- **Holds:**
  - Model, Effort and Context choices
  - switches for Autopilot, Skip permission prompts and Remote session, with a line saying whether Copilot's own setting is on now
  - the sign-in instruction
  - Models offered, one id per line
  - a line with an Add button when the saved list lacks a default model
  - Launch presets, the saved list with a kebab on each row and one form (name, model from the list above, effort the model offers, context tier) that adds a preset or, from Edit, changes one
  - Done
- **Does:**
  - Effort applies only with a model picked here
  - Add appends the missing default models to the saved list
  - a preset is saved as it is added, edited, renamed on its row or removed, and then appears in the Coding launch dropdown; a refusal shows under the field it is about
  - Remote session is applied at each Copilot launch from Coding, Team OS and the Board; off starts sessions unshared
- **Why:** Copilot takes its model at launch from here and has no in-session switch the launcher drives
- **Leads to:** [Launch preset menu](#settingspreset-menu-launch-preset-menu) (preset kebab)

### `settings.chief` Chief sheet

- **Root:** `data-surface`
- **Reach:**
  - Chief in Agents
  - Chief settings in a chief menu
- **Holds:**
  - the chief's model, context and effort, and a worker cap
  - a line naming a saved chief model the Copilot list no longer offers
  - a switch for counting only chief-dispatched sessions
  - Automations - Doorbell, Auto-drain queue, Auto-compact with its threshold, Job failures, Job self-heal and Weekly proposals
  - Done
- **Does:**
  - each switch saves when tapped and no restart is needed
  - model, context and effort apply when the chief next starts; Same as Coding agent follows that sheet
  - a chief model outside the Copilot list starts on Copilot's own pick, with a warning
- **Leads to:** [Chief model menu](#settingschief-model-menu-chief-model-menu) (model)

### `settings.chief-model-menu` Chief model menu

- **Root:** `data-surface`
- **Reach:** the Model picker in the Chief sheet
- **Holds:** Default and every model Settings offers

### `settings.gitlab` GitLab sheet

- **Root:** `data-surface`
- **Reach:** GitLab in Board
- **Holds:**
  - the GitLab group and the GitLab host, blank meaning gitlab.com
  - Done
- **Does:** glab must be signed in to the host on this PC

### `settings.speech` Speech to text sheet

- **Root:** `data-surface`
- **Reach:** Speech to text in Connections
- **Holds:**
  - a Dictation switch, the Base URL, an optional Model and an optional API key that is never shown again
  - Remove key, once a key is set
  - Test connection, with its result line
  - Done
- **Does:**
  - every field saves as it changes
  - Test connection reports Reachable, Refused or Unknown with a reason, using the saved settings
  - with Dictation off no record button shows in any message box
- **States:** the Settings row reads Off, On or No URL

### `settings.preset-menu` Launch preset menu

- **Root:** `data-surface`
- **Reach:** the kebab on a saved launch preset in the GitHub Copilot sheet
- **Holds:**
  - Edit
  - Remove

### `settings.tokens` API tokens sheet

- **Root:** `data-surface`
- **Reach:** API tokens in Connections
- **Holds:**
  - the tokens that each run one job from a link
  - a label, a job and Create token, with the value shown once beside Copy
  - Done
- **States:** empty - No tokens yet, with Create a token

### `settings.folders` Folders sheet

- **Root:** `data-surface`
- **Reach:**
  - Folders in This PC
  - an empty state's Open Settings
- **Holds:**
  - Projects folder, Hidden projects, Apps folder, Team OS folder and fleet-config-lite folder
  - Chat file folders with an Add action and one row per folder
  - Done
- **Leads to:** [Chat folder menu](#settingsoutbox-menu-chat-folder-menu) (a folder's kebab)

### `settings.outbox-menu` Chat folder menu

- **Root:** `data-surface`
- **Reach:** a Chat file folder's kebab
- **Holds:** Remove

### `settings.passkeys` Passkeys sheet

- **Root:** `data-surface`
- **Reach:** Passkeys in This PC
- **Holds:**
  - the enrollment status
  - Enroll this device
  - Done
- **States:** enrolling is only offered while the tray's enrollment window is open

### `settings.terminal` Terminal sheet

- **Root:** `data-surface`
- **Reach:** Terminal in This PC
- **Holds:**
  - Terminal history in lines and the Large file limit in MB
  - Done

## Keys and gestures

Inputs that are not elements.

```mermaid
flowchart LR
  s96["Swipe a row"]
  s97["Long-press hint"]
  s98["Screen view keys"]
  s99["Composer keys"]
  s34(["Screen view · Screen"])
  s26(["On-screen keys · Terminal"])
  s98 -->|"Screen view"| s34
  s99 -->|"keys button"| s26
```

### `keys.swipe-row` Swipe a row

- **Root:** none — a touch gesture on a list row, with no element of its own.
- **Reach:** a horizontal drag on a row that declares swipe actions, on touch or a pen
- **Does:**
  - a partial swipe rests open on the action and a tap commits it; a full swipe or a fast flick commits as released
  - a mouse never swipes, so the row's kebab is the route on a desktop
  - a drag that starts in the leftmost 24px is left to the system back gesture
- **Why:** the kebab is the single-pointer alternative a path gesture needs

### `keys.long-press-hint` Long-press hint

- **Root:** none — a hold on an icon-only control, which toasts its title.
- **Reach:** holding the context ring, the Terminal and Chat toggle or the keys button for half a second
- **Does:**
  - the toast says what the control does, since a touch screen has no hover
  - the hold never triggers the control

### `keys.screen` Screen view keys

- **Root:** none — key and pointer paths into the Screen view, with no element of their own.
- **Reach:** a hardware keyboard, the phone keyboard raised by the keyboard button, a three-finger tap
- **Does:**
  - a hardware key goes to the PC, combined with the keys bar's sticky modifiers
  - the keyboard button and a three-finger tap raise and lower the phone keyboard
  - the keys bar follows the visual viewport while the phone keyboard is up
- **Leads to:** [Screen view](#screenview-screen-view) (Screen view)

### `keys.composer` Composer keys

- **Root:** none — key paths in the shared composer, with no element of their own.
- **Reach:** the composer in the terminal, Chat, the Board drawer and the answer sheet
- **Does:**
  - Send forwards the text over the PTY socket, then a submitting Enter as a separate frame
  - a paste of an image or files queues an upload
- **Leads to:** [On-screen keys](#terminalkeys-popover-on-screen-keys) (keys button)

## Link index

| From | Via | To |
|---|---|---|
| [Primary navigation](#chromenav-primary-navigation) | Board | [Board tab](#boardtab-board-tab) |
| [Primary navigation](#chromenav-primary-navigation) | Code | [Code tab](#codetab-code-tab) |
| [Primary navigation](#chromenav-primary-navigation) | Team | [Team OS tab](#teamtab-team-os-tab) |
| [Primary navigation](#chromenav-primary-navigation) | Apps | [Apps tab](#appstab-apps-tab) |
| [Primary navigation](#chromenav-primary-navigation) | Jobs | [Jobs tab](#jobstab-jobs-tab) |
| [Tab header](#chromehome-head-tab-header) | CPU reading | [PC load sheet](#chromecpu-sheet-pc-load-sheet) |
| [Tab header](#chromehome-head-tab-header) | gear | [Settings](#settingspane-settings) |
| [PC load sheet](#chromecpu-sheet-pc-load-sheet) | Screen View live | [Screen view](#screenview-screen-view) |
| [Code tab](#codetab-code-tab) | Sessions | [Sessions](#codesessions-sessions) |
| [Code tab](#codetab-code-tab) | Projects | [Projects](#codeprojects-projects) |
| [Code tab](#codetab-code-tab) | header | [Tab header](#chromehome-head-tab-header) |
| [Sessions](#codesessions-sessions) | row tap | [Session overlay](#terminaloverlay-session-overlay) |
| [Sessions](#codesessions-sessions) | row kebab | [Session menu](#codesession-menu-session-menu) |
| [Sessions](#codesessions-sessions) | stopped chief kebab | [Stopped chief menu](#codechief-menu-stopped-chief-menu) |
| [Session menu](#codesession-menu-session-menu) | Links | [Links sheet](#terminallinks-sheet-links-sheet) |
| [Session menu](#codesession-menu-session-menu) | Rename | [Rename session](#terminalrename-dialog-rename-session) |
| [Session menu](#codesession-menu-session-menu) | Stop | [Confirm dialog](#chromeconfirm-dialog-confirm-dialog) |
| [Session menu](#codesession-menu-session-menu) | Chief settings | [Chief sheet](#settingschief-chief-sheet) |
| [Stopped chief menu](#codechief-menu-stopped-chief-menu) | Chief settings | [Chief sheet](#settingschief-chief-sheet) |
| [Projects](#codeprojects-projects) | row kebab | [Project menu](#codeproject-menu-project-menu) |
| [Projects](#codeprojects-projects) | preset dropdown | [Launch preset menu](#codepreset-menu-launch-preset-menu) |
| [Projects](#codeprojects-projects) | Sync all | [Sync all sheet](#codegit-sync-sheet-sync-all-sheet) |
| [Projects](#codeprojects-projects) | History | [History sheet](#codehistory-sheet-history-sheet) |
| [Projects](#codeprojects-projects) | Open Settings | [Folders sheet](#settingsfolders-folders-sheet) |
| [Projects](#codeprojects-projects) | row tap | [Session overlay](#terminaloverlay-session-overlay) |
| [Project menu](#codeproject-menu-project-menu) | Show changes | [Changes overlay](#codechanges-overlay-changes-overlay) |
| [Changes overlay](#codechanges-overlay-changes-overlay) | file kebab | [Changed file menu](#codechanges-file-menu-changed-file-menu) |
| [Session overlay](#terminaloverlay-session-overlay) | Terminal mode | [Terminal](#terminalhost-terminal) |
| [Session overlay](#terminaloverlay-session-overlay) | model chip | [Switch model sheet](#terminalmodel-sheet-switch-model-sheet) |
| [Session overlay](#terminaloverlay-session-overlay) | Chat mode | [Chat pane](#chatoverlay-chat-pane) |
| [Session overlay](#terminaloverlay-session-overlay) | bar kebab | [Overlay session menu](#terminalsession-menu-overlay-session-menu) |
| [Terminal](#terminalhost-terminal) | keys button | [On-screen keys](#terminalkeys-popover-on-screen-keys) |
| [Terminal](#terminalhost-terminal) | attach button | [Attach menu](#terminalattach-menu-attach-menu) |
| [Overlay session menu](#terminalsession-menu-overlay-session-menu) | Rename | [Rename session](#terminalrename-dialog-rename-session) |
| [Overlay session menu](#terminalsession-menu-overlay-session-menu) | Links | [Links sheet](#terminallinks-sheet-links-sheet) |
| [Overlay session menu](#terminalsession-menu-overlay-session-menu) | Changed files | [Changes overlay](#codechanges-overlay-changes-overlay) |
| [Overlay session menu](#terminalsession-menu-overlay-session-menu) | Stop and kill | [Confirm dialog](#chromeconfirm-dialog-confirm-dialog) |
| [Chat pane](#chatoverlay-chat-pane) | queued chip | [Queued messages](#chatqueued-sheet-queued-messages) |
| [Chat pane](#chatoverlay-chat-pane) | image thumbnail | [Image lightbox](#chatlightbox-image-lightbox) |
| [Chat pane](#chatoverlay-chat-pane) | HTML document card | [HTML viewer](#chathtml-viewer-html-viewer) |
| [Chat pane](#chatoverlay-chat-pane) | the agent asks | [Question and plan cards](#chatdecision-cards-question-and-plan-cards) |
| [Chat pane](#chatoverlay-chat-pane) | a finished turn | [Turn cards and step lines](#chatturn-cards-turn-cards-and-step-lines) |
| [Turn cards and step lines](#chatturn-cards-turn-cards-and-step-lines) | file row | [Changes overlay](#codechanges-overlay-changes-overlay) |
| [Screen view](#screenview-screen-view) | Quality | [Quality sheet](#screenquality-sheet-quality-sheet) |
| [Screen view](#screenview-screen-view) | Paste to PC | [Paste to PC sheet](#screenpaste-sheet-paste-to-pc-sheet) |
| [Screen view](#screenview-screen-view) | readout switch | [Readout](#screenreadout-readout) |
| [Apps tab](#appstab-apps-tab) | Running | [Running](#appsrunning-running) |
| [Apps tab](#appstab-apps-tab) | Other ports | [Other ports card](#appsother-ports-card-other-ports-card) |
| [Apps tab](#appstab-apps-tab) | Apps | [Apps list](#appsapps-apps-list) |
| [Apps tab](#appstab-apps-tab) | Trays | [Trays](#appstrays-trays) |
| [Apps tab](#appstab-apps-tab) | Mini apps | [Mini apps](#appsmini-apps-mini-apps) |
| [Running](#appsrunning-running) | row kebab | [Running app menu](#appsrunning-menu-running-app-menu) |
| [Running](#appsrunning-running) | Other ports | [Other ports sheet](#appsother-ports-sheet-other-ports-sheet) |
| [Running app menu](#appsrunning-menu-running-app-menu) | Stop | [Confirm dialog](#chromeconfirm-dialog-confirm-dialog) |
| [Other ports card](#appsother-ports-card-other-ports-card) | row kebab | [Listener menu](#appslistener-menu-listener-menu) |
| [Other ports sheet](#appsother-ports-sheet-other-ports-sheet) | row kebab | [Listener menu](#appslistener-menu-listener-menu) |
| [Listener menu](#appslistener-menu-listener-menu) | Stop process | [Confirm dialog](#chromeconfirm-dialog-confirm-dialog) |
| [Apps list](#appsapps-apps-list) | row kebab | [App menu](#appsapp-menu-app-menu) |
| [Apps list](#appsapps-apps-list) | Scan for new apps | [New entries sheet](#appsscan-sheet-new-entries-sheet) |
| [App menu](#appsapp-menu-app-menu) | Rename | [Rename](#appsrename-dialog-rename) |
| [Jobs tab](#jobstab-jobs-tab) | Next up | [Next up](#jobsnext-up-next-up) |
| [Jobs tab](#jobstab-jobs-tab) | Jobs | [Jobs list](#jobslist-jobs-list) |
| [Jobs tab](#jobstab-jobs-tab) | wide layout | [This week](#jobsweek-this-week) |
| [Next up](#jobsnext-up-next-up) | Next up title | [Schedule sheet](#jobsagenda-sheet-schedule-sheet) |
| [Jobs list](#jobslist-jobs-list) | filters | [Sort and filter](#jobssort-sheet-sort-and-filter) |
| [Jobs list](#jobslist-jobs-list) | Add job | [Add or edit job](#jobsjob-dialog-add-or-edit-job) |
| [Jobs list](#jobslist-jobs-list) | row tap | [Job sheet](#jobsjob-sheet-job-sheet) |
| [Jobs list](#jobslist-jobs-list) | row kebab | [Job menu](#jobsjob-menu-job-menu) |
| [Job menu](#jobsjob-menu-job-menu) | Run now | [Run job](#jobsrun-dialog-run-job) |
| [Job menu](#jobsjob-menu-job-menu) | Edit | [Add or edit job](#jobsjob-dialog-add-or-edit-job) |
| [Job menu](#jobsjob-menu-job-menu) | Remove | [Confirm dialog](#chromeconfirm-dialog-confirm-dialog) |
| [Job sheet](#jobsjob-sheet-job-sheet) | Run now | [Run job](#jobsrun-dialog-run-job) |
| [Team OS tab](#teamtab-team-os-tab) | Skills | [Skills](#teamskills-skills) |
| [Team OS tab](#teamtab-team-os-tab) | Weekly recap | [Weekly recap](#teamrecap-weekly-recap) |
| [Skills](#teamskills-skills) | model picker | [Skill model menu](#teammodel-menu-skill-model-menu) |
| [Skills](#teamskills-skills) | row kebab | [Skill menu](#teamskill-menu-skill-menu) |
| [Skills](#teamskills-skills) | Open Settings | [Folders sheet](#settingsfolders-folders-sheet) |
| [Skill menu](#teamskill-menu-skill-menu) | Read | [Skill sheet](#teamskill-sheet-skill-sheet) |
| [Skill sheet](#teamskill-sheet-skill-sheet) | a file | [Skill file](#teamdoc-sheet-skill-file) |
| [Skill file](#teamdoc-sheet-skill-file) | Delete log | [Confirm dialog](#chromeconfirm-dialog-confirm-dialog) |
| [Board tab](#boardtab-board-tab) | project combo | [Project combo list](#boardrepo-menu-project-combo-list) |
| [Board tab](#boardtab-board-tab) | Backlog | [Backlog](#boardbacklog-backlog) |
| [Board tab](#boardtab-board-tab) | Since you last looked | [Since you last looked](#boardjournal-since-you-last-looked) |
| [Board tab](#boardtab-board-tab) | Chief's plan | [Chief's plan](#boardchief-plan-chiefs-plan) |
| [Board tab](#boardtab-board-tab) | Bot's turn | [Bot's turn](#boardbots-turn-bots-turn) |
| [Board tab](#boardtab-board-tab) | Your turn | [Your turn](#boardyour-turn-your-turn) |
| [Board tab](#boardtab-board-tab) | MRs and jobs | [MRs and jobs](#boardmrs-jobs-mrs-and-jobs) |
| [Board tab](#boardtab-board-tab) | Done today | [Done today](#boarddone-done-today) |
| [Backlog](#boardbacklog-backlog) | Start with | [Start with menu](#boardmodel-menu-start-with-menu) |
| [Backlog](#boardbacklog-backlog) | row kebab | [Issue menu](#boardissue-menu-issue-menu) |
| [Chief's plan](#boardchief-plan-chiefs-plan) | a row's note | [Plan note](#boardplan-note-sheet-plan-note) |
| [Chief's plan](#boardchief-plan-chiefs-plan) | questions | [Answer the chief](#boardanswer-sheet-answer-the-chief) |
| [Bot's turn](#boardbots-turn-bots-turn) | row tap | [Session drawer](#boarddrawer-session-drawer) |
| [Your turn](#boardyour-turn-your-turn) | session row | [Session drawer](#boarddrawer-session-drawer) |
| [Your turn](#boardyour-turn-your-turn) | Answer | [Answer the chief](#boardanswer-sheet-answer-the-chief) |
| [Session drawer](#boarddrawer-session-drawer) | kebab | [Drawer menu](#boarddrawer-menu-drawer-menu) |
| [Session drawer](#boarddrawer-session-drawer) | Terminal | [Session overlay](#terminaloverlay-session-overlay) |
| [Session drawer](#boarddrawer-session-drawer) | Chat | [Chat pane](#chatoverlay-chat-pane) |
| [Drawer menu](#boarddrawer-menu-drawer-menu) | Stop | [Confirm dialog](#chromeconfirm-dialog-confirm-dialog) |
| [Settings](#settingspane-settings) | the groups | [Settings rows](#settingsgroups-settings-rows) |
| [Settings](#settingspane-settings) | a document row | [Document sheet](#settingsdocument-document-sheet) |
| [Settings](#settingspane-settings) | Coding agent | [GitHub Copilot sheet](#settingscopilot-github-copilot-sheet) |
| [Settings](#settingspane-settings) | Chief | [Chief sheet](#settingschief-chief-sheet) |
| [Settings](#settingspane-settings) | GitLab | [GitLab sheet](#settingsgitlab-gitlab-sheet) |
| [Settings](#settingspane-settings) | Speech to text | [Speech to text sheet](#settingsspeech-speech-to-text-sheet) |
| [Settings](#settingspane-settings) | API tokens | [API tokens sheet](#settingstokens-api-tokens-sheet) |
| [Settings](#settingspane-settings) | Folders | [Folders sheet](#settingsfolders-folders-sheet) |
| [Settings](#settingspane-settings) | Passkeys | [Passkeys sheet](#settingspasskeys-passkeys-sheet) |
| [Settings](#settingspane-settings) | Terminal | [Terminal sheet](#settingsterminal-terminal-sheet) |
| [GitHub Copilot sheet](#settingscopilot-github-copilot-sheet) | preset kebab | [Launch preset menu](#settingspreset-menu-launch-preset-menu) |
| [Chief sheet](#settingschief-chief-sheet) | model | [Chief model menu](#settingschief-model-menu-chief-model-menu) |
| [Folders sheet](#settingsfolders-folders-sheet) | a folder's kebab | [Chat folder menu](#settingsoutbox-menu-chat-folder-menu) |
| [Screen view keys](#keysscreen-screen-view-keys) | Screen view | [Screen view](#screenview-screen-view) |
| [Composer keys](#keyscomposer-composer-keys) | keys button | [On-screen keys](#terminalkeys-popover-on-screen-keys) |
