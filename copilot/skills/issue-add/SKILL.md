---
name: issue-add
description: Turn a rough idea, brain-dump, or transcript into a well-formed GitLab issue — researches the codebase, drafts it like a senior developer, labels it, self-assigns, and creates it. Use when capturing new work, e.g. "/issue-add <paste your idea>". Pairs with /issue-start and /issue-finish.
---

# issue-add (lite, GitLab)

**Goal:** Take whatever the user pastes — a clean idea, a rambling brain-dump,
a voice transcript — and file **one** well-formed GitLab issue: self-contained,
researched against this codebase, correctly scoped, ready to hand off cold.

The issue is **created directly** once drafted — no approval checkpoint.

## Arguments

Everything after `/issue-add` is the raw input. If nothing was pasted, ask the
user for the idea and stop until they provide it.

## Steps

Run in order. If a step fails, print a short error and stop.

### 1. Context

- `git rev-parse --is-inside-work-tree` must be `true`, else stop: "Not inside a git repository."
- Read the project's `README.md` (and `AGENTS.md`/`copilot-instructions.md` if present) for layout and conventions.

### 2. Research

Ground every claim in the code: find the files/functions the idea touches and
reference them as `path/to/file.py:line`. If the idea is ambiguous, pick the
most plausible reading and note the assumption in the issue body.

### 3. Draft

- **Title:** imperative, specific, ≤ 80 chars, no trailing period.
- **Body sections:** `## Context` (why, grounded in code refs), `## Proposal`
  (what to change, concrete), `## Acceptance` (checkbox list a reviewer can
  verify). Single-paragraph lines — no hard wraps.

### 4. Label + create

- Type label from: `bug`, `enhancement`, `refactor`, `documentation`, `chore`,
  `test`, `performance` — create it first if the project lacks it
  (`glab label create -n <name>`).
- Create and self-assign:

```
glab issue create --title "<title>" --description "<body>" --label <type> --assignee @me
```

### 5. Report

Print the issue URL and one-line summary. Suggest `/issue-start <N>` as the
next step.
