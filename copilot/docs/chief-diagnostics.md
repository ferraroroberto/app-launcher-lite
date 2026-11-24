# Chief diagnostics run

Follow this when the owner asks for a diagnostics run. It checks this machine's chief setup the same way on every computer and returns one redacted Markdown report that can be brought back and turned into fixes. `chief-ops` stands for the launcher command in the chief skill.

## Rules for the whole run

- Read-only, except the optional round trip below: no `git` writes, no restarts, no deletes, no `--apply`, no `plan`, `note` or `file` writes, no messages to other sessions.
- A fact you could not establish is `unknown`. Never write `pass` for it, and never fill it from memory.
- Redact before you write: the home folder is `<home>`, the user name `<user>`, the host name `<host>`. No session transcripts, job output, tokens or issue bodies go into the report.

## Step 1: doctor

Run `chief-ops doctor`. It prints one `CHECK <area>.<name>=pass|fail|unknown ...` line per fact (install, verbs, mailbox, logs), up to 10 `LOG ...` groups, and a last line `DOCTOR=pass|fail|unknown fails=<n> unknowns=<n>`. Its output is already redacted. Keep every line for the report. A missing last line means the run did not finish: the verdict is `unknown`.

## Step 2: the live round trip (only on the owner's OK)

Ask first, in this turn's reply: "The round trip opens two throwaway Copilot sessions in project `<P>` and stops only those two. OK?" Run it only when the owner says yes to that question. Otherwise mark every round-trip row `skipped (no OK)`.

`<P>` is a Coding-tab project with no live session in it (`chief-ops sessions`). Write the message `Reply with the single word OK, then stop.` to a scratch file.

1. **Managed session.** `chief-ops open <P> --lane --message-file <file>`. It is marked chief-managed before its first message, so its turn end must reach the mailbox. Record `OPENED session=<sid>`, `DISPATCH_ID=` and the three fact lines.
2. **Turn-end and doorbell.** Every 15 s for up to 3 minutes run `chief-ops inbox --count`. The doorbell is on when `wake` printed `doorbell=on`: then the ring itself arrives as `⟳ chief-inbox: pending` in your terminal, so note whether it came. When `PENDING` is above 0, run `chief-ops inbox`, find the `kind=turn-end` event for `<sid>`, and run its `ACK=` command. No event in 3 minutes is a fail.
3. **The card.** `chief-ops board --json`: find the row for `<sid>` and record its column and status. A finished turn reads as your turn (`your_turn`) with an idle status.
4. **Stop and session-end.** `chief-ops stop <sid>`. Then poll `inbox --count` again for up to 2 minutes for that session's `kind=session-end` event; ack it. No event is a fail.
5. **Plain session (expected silence).** `chief-ops open <P> --message-file <file>` with no `--lane`. A plain session is never chief-managed, so no turn-end event is expected. Wait 90 s after `DELIVERED`, check `inbox --count` stays 0 for that sid, then `chief-ops stop <sid>`. An event from it is a fail; silence is a pass.

Stop only the two sessions this step opened. If one will not stop, report its sid prefix and leave it.

## Step 3: your own failed calls

Look back over this session's own tool calls (the chief's transcript as you see it, not the files of others): every call that exited non-zero, printed `REFUSED=`, `UNCONFIRMED=`, `UNREACHABLE=` or `ERROR:`, or that you had to retry with other arguments. For each, name the skill or `docs/chief.md` line that led you to it and say what was misleading. Count them.

## Step 4: the report

Reply with one Markdown report, in this order:

1. **Verdict:** the `DOCTOR=` line, then the round trip as `pass`, `fail`, `unknown` or `skipped (no OK)`.
2. **Counts:** checks passed, failed, unknown; log failure groups; failed tool calls of your own.
3. **Top 5 problems**, worst first, each with its evidence (the `CHECK` or `LOG` line) and a suggested fix (for example "run `copilot/install.py`", "update the checkout", "re-run `install.py --migrate`", "file an issue on app-launcher-lite: ...").
4. **Per-check table:** `| Area | Check | Result | Detail |`, one row per `CHECK` line and one per round-trip step.
5. **Log groups:** the `LOG` lines as a table.
6. **Skill lines that misled you**, from step 3.

End the turn after the report. Filing issues from it is the owner's call.
