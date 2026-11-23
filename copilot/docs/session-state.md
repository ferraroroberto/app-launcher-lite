# Session status: a declared state machine

Each live Copilot session has at most one row in `sessions-state.json`, and the row's `status` says whose turn it is. The status is a small state machine; `hooks/session_state.py` declares it in one place so no other code has to guess the legal moves.

## Why it is declared

The status is written by event name, and the rules for it (which event sets which status, which one never overwrites a real status, which one deletes the row) used to be spread across a lookup table, a set of exempt events and a branch in `main()`. A fifth status or one more special case would have been another scattered edit. Now the statuses are the members of `SessionStatus`, and `next_transition` is the only function that maps an event to a change of the row. Both live in `hooks/session_state.py`; `.fleet.toml` declares them as the `session-status` machine, so the conventions ratchet reports any other module that re-spells the statuses.

## States

| Status | Wire value | Meaning |
|---|---|---|
| `SessionStatus.IDLE` | `idle` | The session started and no turn has been handed over yet. |
| `SessionStatus.WORKING` | `working` | The user handed over a turn and the agent has not stopped. |
| `SessionStatus.NEEDS_YOU` | `needs-you` | The agent finished a turn and waits for the user. |

"No row" is the fourth position: the session has not been seen, or it ended. The set is closed: a new status is a new member, handled in `next_transition`.

## Transitions

`next_transition(existing, event)` takes the row's current status (`None` for no row) and the event name, and returns what to do with the row, or `None` to ignore the event.

| Event | No row | `idle` | `working` | `needs-you` |
|---|---|---|---|---|
| `userPromptSubmitted` | write `working` | write `working` | write `working` | write `working` |
| `agentStop` | write `needs-you` | write `needs-you` | write `needs-you` | write `needs-you` |
| `sessionStart` | write `idle` | refresh | refresh | refresh |
| `sessionEnd` | nothing to delete | delete | delete | delete |
| any other event | ignored | ignored | ignored | ignored |

- **Write** replaces the row with the new status and keeps a `transcript_path` learned earlier.
- **Refresh** keeps the row exactly as it is and only moves `updated_at`. `sessionStart` never downgrades: in non-interactive runs it fires after `userPromptSubmitted`, so writing `idle` there would flip a correct `working` row back.
- **A stored status outside the set** (a hand-edited or damaged file) counts as no usable status: the next `sessionStart` rewrites the row as `idle`, and the other events behave as for any row. The hook never raises on it and always exits 0.
- **A sub-agent's own events write nothing.** A sub-agent (a `task` call, autopilot's completion review) runs through its parent's hooks: its `userPromptSubmitted` and `agentStop` arrive with `sessionId` set to its tool-call id and `parentAgentId` naming the parent, under the parent's inherited launcher env. A row written for it would claim the parent's Board card and read `needs-you` while the parent is still mid-turn, so `main()` returns before any write or chief-mailbox event when the payload carries `parentAgentId`.
- A hard kill never fires `sessionEnd`; the webapp's reconcile pass stamps those rows `ended` (a value this module never writes, so it is not a `SessionStatus` member), and the 24-hour prune on the next write removes them. A resumed session's next event replaces its ended row.

## Who reads it

The Board in app-launcher-lite reads `sessions-state.json`. `needs-you` only records that a turn ended; whether that end needs a person is read from the session's `events.jsonl` (`src/board/copilot_status.py`): a background shell still running reads `waiting-on-task`, a turn that wrote no reply `turn-failed`, a pending `ask_user` `awaiting-decision`, a clean stop `idle-finished`, and an unreadable transcript `unknown`. The hook does not read the transcript for the row, because the Board re-reads it on every poll and so follows the shell's completion without another hook event. This module is the only writer, and the file's shape and the wire values are unchanged by the declaration.

## Adding a reader or an event

A new event is a new branch in `next_transition` plus a row in its table test (`tests/test_session_state.py`), and a new status is a new `SessionStatus` member plus the rows it needs. Code that needs to know what an event does to a row calls `next_transition`; it does not compare status strings or event names itself.
