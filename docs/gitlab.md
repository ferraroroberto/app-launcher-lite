# GitLab

GitLab is the only forge the launcher knows. The Board reads it through the `glab` CLI, and the companion repo's `/issue-start` and `/issue-yolo` skills use `glab` for their own issue work.

## Verified live and not

The client in `src/clients/gitlab.py` ran against gitlab.com with `glab` 1.131 (browser sign-in, a private project in a private group). Its fixtures in `tests/unit/board/test_gitlab_client.py` follow what came back.

| Item | State |
| --- | --- |
| Open group issues (`groups/<group>/issues?state=opened`), including `references.full`, `web_url`, `labels` as strings | verified |
| Closed group issues with `updated_after`, `closed_at` as a millisecond `Z` stamp, landing in Done | verified |
| Open and merged group merge-request reads (the endpoints and query strings are accepted) | verified to answer; the group held none, so the row shape is `unknown` |
| Merge-request `draft` and `description`, and the `Closes #N` / `VISUAL_PROOF=` pairing | `unknown` |
| A merged merge request carries `merged_at` as a millisecond `Z` stamp (group and project reads) | verified |
| `merged_after` on `projects/<path>/merge_requests?state=merged` filters: a stamp in the future answers `[]`, one in the past answers the merged rows | verified |
| `created_after` on `projects/<path>/issues` filters the same way | verified |
| `closed_after` on `projects/<path>/issues?state=closed` | verified **ignored**: a stamp in the future still answered a closed issue, so closed issues are asked for by `updated_after` and kept by their own `closed_at` |
| A project path with subgroups as one encoded segment (`group%2Fsub%2Fproject`) for the History reads | verified for a one-level path; deeper nesting `unknown` |
| `GET user` for the assignee id | verified |
| File an issue (assigned to the signed-in user) and add a note | verified |
| Closing an issue (`state_event=close`) | verified; the client has no close call, so a live check used `glab api` directly |
| A group with no issues or merge requests refreshes with no error | verified |
| A page of 100 (`truncated`) against a real group, a subgroup-nested project, a self-hosted `gitlab_host` | `unknown` |
| Project list (`groups/<group>/projects?include_subgroups=true&archived=false`) with `id`, `path`, `path_with_namespace`, `default_branch` and `builds_access_level` | verified |
| Pipelines on the default branch (`projects/<id>/pipelines?ref=<default>&order_by=id&sort=desc&per_page=20`): a project with none answers `200 []` | verified (empty); the group's projects held no pipeline |
| The same read for a ref GitLab does not know | verified: `200 []`, the same as "no pipelines", so the ref is named from the project row, never guessed |
| `projects/<id>/pipelines/latest?ref=<default>` on a project with no pipeline | `403 Forbidden`; not used |
| Reading a pipeline's row shape and `status` values (`id`, `iid`, `sha`, `ref`, `status`, `source`, `created_at`, `web_url`) | verified against a public project's pipelines (`success` and `failed`); not yet against a pipeline of the group |
| A red or green pipeline of the group itself, `cancelled`/`skipped` rows, a project with CI switched off, a self-hosted host | `unknown` |
| `glab mr merge --auto-merge` through the chief's merge verb | verified on a GitLab test project through the companion repo's copy of the verb: one request merged and read back clean, two conflicting requests refused with GitLab's reason; this tree's copy is unit-tested and not re-run live from here |
| Labels | `unknown` (not read or written by the client today) |
| The two-remote mirror and `scripts/sync_gitlab_mirror.py` | verified (push, level re-run); see below |
| An Android Git client | `unknown` |

## Setup

1. Install `glab` and sign in as the owner with `glab auth login`; see [installing-agent-clis.md](installing-agent-clis.md). The token lives in glab's credential store. Never put a token in this repo, in `config/`, in an issue or in a commit.
2. In Settings (or `config/webapp_config.json`) set `gitlab_group`, the group or user namespace whose projects the Board reads (subgroups included), and `gitlab_host` for a self-hosted instance. `gitlab_host` rides glab's `GITLAB_HOST` environment variable on the Board's reads and on every chief and owner-check call; empty takes the host of the checkout's `origin` when that is not GitHub, else gitlab.com. `glab` must be signed in to that host (`glab auth status` lists the token per host); Lite never reads, stores or logs it. A `glab` failure from the chief ends with `(tried host <host>, project <path>)`, so a 404 from a wrong host or group names both. The chief's argv and environment are pinned by `copilot/tests/test_gitlab_host.py`; a live self-hosted instance is `unknown`. With no group set the Board's issue columns stay dormant and show a Settings hint.
3. Check one real response against the fixtures when you add a new kind of read: `glab api "groups/<group>/issues?state=opened&per_page=1"`. The client relies on `references.full`, `closed_at` and `web_url`. If the instance answers differently, fix the fixtures and the client together.

## What the Board reads

Every read is `glab api` against REST v4, one subprocess per call with a 20 s timeout and no pagination (a full page of 100 is recorded as `truncated`):

- **Group issues:** open issues across the group. Issues carrying a non-actionable label are kept as records rather than cards.
- **Open merge requests:** `groups/<group>/merge_requests?state=opened`, most recently updated first.
- **Merged merge requests, today:** the MRs merged on the local day, used to pair each closed issue with the MR that closed it. The MR description's `VISUAL_PROOF=<state>` token is read for the proof state shown on a Done card.
- **Issues closed today:** the Done column (`closed_at` inside the local day).

- **Default-branch pipelines:** the group's live projects (archived and CI-off ones left out), then one `projects/<id>/pipelines?ref=<default>` read each, four in flight. The default branch comes from the project row: GitLab answers `[]` for a ref it does not know, so a guessed branch would read as "no pipelines". The latest finished pipeline (`failed` or `success`) decides: `red`, `green`, or `unknown` when the read failed or the row names no default branch. A project with no finished pipeline is not a row. The Board draws a red project as one `CI failed` row and an unreadable one as `CI unknown`; see [board.md](board.md#red-pipelines). No pipeline-file check is made first: the empty answer already says "no pipeline".

**No forge call ever runs on a poll.** `refresh()` runs only on the Board's ↻ button or once on a stale tab activation (with a 60 s cooldown); the 5 s poll reads the in-memory snapshot. A failed refresh keeps the previous data and surfaces an `error` badge; a never-filled cache is "never fetched", not "nothing open".

## What the History sheet reads

The [History sheet](history.md) reads one project at a time, by the project's full path taken from its local GitLab remote, three `glab api` calls each, only when its Refresh is tapped:

- **Merged:** `projects/<path>/merge_requests?state=merged&merged_after=<since>&per_page=100`, counted by `merged_at`.
- **Opened:** `projects/<path>/issues?created_after=<since>&per_page=100`, counted by `created_at`.
- **Closed:** `projects/<path>/issues?state=closed&updated_after=<since>&per_page=100`, counted by `closed_at` and only when that is not before `<since>`, because `closed_after` is ignored (see the table above).

A page of 100 is recorded as `truncated` for that project and shown on the sheet; the reads do not paginate. A failed read keeps the project's previous days and marks the source `stale`.

## What the Board writes

- **File an issue:** `POST projects/<group>%2F<repo>/issues`, assigned to the signed-in user (`GET user` supplies the id).
- **Issue note:** `POST projects/<group>%2F<repo>/issues/<n>/notes`, the body sent on stdin.

Both are used by the Board's file-an-issue flow and by anything that answers on an issue. Verified live.

## Owner checks

The owner-check feature (shipped work to validate, steps owed before a start) is meant to use GitLab scoped labels. It is opt-in and **not yet live**: the label scheme and the helper that reads and clears the labels are in `copilot/skills/_lib/owner_check/`. Until then the Board's owner-check card stays empty.

## Merging

The Board merges nothing. The chief's merge verb in `copilot/` runs `glab mr merge --auto-merge`. On a GitLab test project the companion repo's copy of the verb merged one request and read it back clean, and refused two conflicting requests with GitLab's own reason; this tree's copy carries the same reason handling and is unit-tested, but has not been re-run live from here. A self-hosted host is `unknown` (see [Status](../README.md#status)).

## Two remotes for this repo

This repo itself can live on both forges: GitHub `origin` plus a private GitLab project of the same name as a second remote.

```bash
git remote add gitlab <url-of-the-private-gitlab-project>
git push gitlab main
```

Push branches the same way (`git push gitlab <branch>`). Never force-push either remote. `origin` stays the default for fetch; `gitlab` is a mirror pushed to deliberately.

### Keeping the mirror level

gitlab.com's free tier has no pull mirroring, so `scripts/sync_gitlab_mirror.py` is the mirror: it fetches `origin`, then pushes `main` and tags to `gitlab`, fast-forward only, never forcing. If GitLab's `main` holds a commit GitHub lacks it pushes nothing and says so; reconcile on GitHub first. Exit codes: `0` level or pushed, `1` refused (diverged mirror, or a tag that differs), `2` not established (a fetch, lookup or push failed for another reason). A schedule that reads exit `2` as success hides an outage, so treat it as a failure.

```powershell
.\.venv\Scripts\python.exe scripts\sync_gitlab_mirror.py
```

It runs on a schedule as a launcher Job named `gitlab-mirror-sync` (Jobs tab): kind `python`, script path the absolute path of `scripts/sync_gitlab_mirror.py` in the checkout the tray serves, no arguments, hourly. The Job is in place on the owner's machine; on a new machine, add the same row to `config/jobs.json` (or through the Jobs tab). The push uses `glab`'s credential helper, so the Job must run as the user who signed in with `glab auth login`; a session-less Job (`session_less: true`) is not verified against the keyring.

The Jobs tab reads any non-zero exit as a failed run (a red `failed` chip): the script is a plain Job with no `exit_outcomes`, so exit `1` and exit `2` both show red, never success. `tests/unit/repo/test_sync_gitlab_mirror.py` pins that reading. To see a run go red on purpose, give a throwaway copy of the Job the argument `--mirror no-such-remote`, which exits `2`.

## Working from an Android Git client

Nothing in the launcher assumes every push comes from the PC: the Board reads GitLab, not local git. A phone can push too. With an Android Git client such as **MGit**:

1. In GitLab, as the owner, create a **personal access token** with scope `read_repository` and `write_repository`. Keep it in the client's credential store; never paste it into a repo file, an issue or a chat.
2. Clone over **HTTPS** with the GitLab project URL (`https://<host>/<group>/<project>.git`), using your GitLab username and the token as the password.
3. Branch, edit, commit and push to that branch from the client. Do not force-push.
4. On the PC, `git fetch` and pull the branch (or the merged `main`) before the next session works on it, so the local checkout the Board's ▶/⚡ buttons launch in matches GitLab.

The flow is not yet verified on a handset; the client's exact menus are its own.
