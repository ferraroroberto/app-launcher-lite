# GitLab

GitLab is the only forge the launcher knows. The Board reads it through the `glab` CLI, and the companion repo's `/issue-start` and `/issue-yolo` skills use `glab` for their own issue work.

## Verified live and not

The client in `src/clients/gitlab.py` ran against gitlab.com with `glab` 1.131 (browser sign-in, a private project in a private group). Its fixtures in `tests/unit/board/test_gitlab_client.py` follow what came back.

| Item | State |
| --- | --- |
| Open group issues (`groups/<group>/issues?state=opened`), including `references.full`, `web_url`, `labels` as strings | verified |
| Closed group issues with `updated_after`, `closed_at` as a millisecond `Z` stamp, landing in Done | verified |
| Open and merged group merge-request reads (the endpoints and query strings are accepted) | verified to answer; the group held none, so the row shape is `unknown` |
| Merge-request `draft`, `merged_at`, `description` and the `Closes #N` / `VISUAL_PROOF=` pairing | `unknown` |
| `GET user` for the assignee id | verified |
| File an issue (assigned to the signed-in user) and add a note | verified |
| Closing an issue (`state_event=close`) | verified; the client has no close call, so a live check used `glab api` directly |
| A group with no issues or merge requests refreshes with no error | verified |
| A page of 100 (`truncated`) against a real group, a subgroup-nested project, a self-hosted `gitlab_host` | `unknown` |
| Pipelines, labels, `glab mr merge` | `unknown` (not read or written by the client today) |
| The two-remote mirror and `scripts/sync_gitlab_mirror.py` | verified (push, level re-run); see below |
| An Android Git client | `unknown` |

## Setup

1. Install `glab` and sign in as the owner with `glab auth login`; see [installing-agent-clis.md](installing-agent-clis.md). The token lives in glab's credential store. Never put a token in this repo, in `config/`, in an issue or in a commit.
2. In Settings (or `config/webapp_config.json`) set `gitlab_group`, the group or user namespace whose projects the Board reads (subgroups included), and `gitlab_host` for a self-hosted instance. `gitlab_host` rides glab's `GITLAB_HOST` environment variable; empty means glab's default context. With no group set the Board's issue columns stay dormant and show a Settings hint.
3. Check one real response against the fixtures when you add a new kind of read: `glab api "groups/<group>/issues?state=opened&per_page=1"`. The client relies on `references.full`, `closed_at` and `web_url`. If the instance answers differently, fix the fixtures and the client together.

## What the Board reads

Every read is `glab api` against REST v4, one subprocess per call with a 20 s timeout and no pagination (a full page of 100 is recorded as `truncated`):

- **Group issues:** open issues across the group. Issues carrying a non-actionable label are kept as records rather than cards.
- **Open merge requests:** `groups/<group>/merge_requests?state=opened`, most recently updated first.
- **Merged merge requests, today:** the MRs merged on the local day, used to pair each closed issue with the MR that closed it. The MR description's `VISUAL_PROOF=<state>` token is read for the proof state shown on a Done card.
- **Issues closed today:** the Done column (`closed_at` inside the local day).

**No forge call ever runs on a poll.** `refresh()` runs only on the Board's ↻ button or once on a stale tab activation (with a 60 s cooldown); the 5 s poll reads the in-memory snapshot. A failed refresh keeps the previous data and surfaces an `error` badge; a never-filled cache is "never fetched", not "nothing open".

## What the Board writes

- **File an issue:** `POST projects/<group>%2F<repo>/issues`, assigned to the signed-in user (`GET user` supplies the id).
- **Issue note:** `POST projects/<group>%2F<repo>/issues/<n>/notes`, the body sent on stdin.

Both are used by the Board's file-an-issue flow and by anything that answers on an issue. Verified live.

## Owner checks

The owner-check feature (shipped work to validate, steps owed before a start) is meant to use GitLab scoped labels. It is opt-in and **not yet live**: the label scheme and the helper that reads and clears the labels ship with fleet-config-lite. Until then the Board's owner-check card stays empty.

## Merging

The Board merges nothing. The chief's merge verb in fleet-config-lite runs `glab mr merge --auto-merge`; its flags are unit-tested and `unknown` against a live GitLab (see [Status](../README.md#status)).

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

Run it on a schedule as a launcher Job (Jobs tab): kind `python`, script path the absolute path of `scripts/sync_gitlab_mirror.py` in this checkout, no arguments, an hourly or daily schedule. The push uses `glab`'s credential helper, so the Job must run as the user who signed in with `glab auth login`; a session-less Job (`session_less: true`) is not verified against the keyring. Creating the Job is the owner's step.

## Working from an Android Git client

Nothing in the launcher assumes every push comes from the PC: the Board reads GitLab, not local git. A phone can push too. With an Android Git client such as **MGit**:

1. In GitLab, as the owner, create a **personal access token** with scope `read_repository` and `write_repository`. Keep it in the client's credential store; never paste it into a repo file, an issue or a chat.
2. Clone over **HTTPS** with the GitLab project URL (`https://<host>/<group>/<project>.git`), using your GitLab username and the token as the password.
3. Branch, edit, commit and push to that branch from the client. Do not force-push.
4. On the PC, `git fetch` and pull the branch (or the merged `main`) before the next session works on it, so the local checkout the Board's ▶/⚡ buttons launch in matches GitLab.

The flow is not yet verified on a handset; the client's exact menus are its own.
