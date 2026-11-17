# GitLab

GitLab is the only forge the launcher knows. The Board reads it through the `glab` CLI, and the companion repo's `/issue-start` and `/issue-yolo` skills use `glab` for their own issue work.

**Status: nothing on this page is verified live.** `glab` is not installed on the build PC yet. The client in `src/clients/gitlab.py` is written against GitLab's documented REST v4 API, and its fixtures in `tests/unit/board/test_gitlab_client.py` are that documentation's JSON, not a captured instance. Each item below carries the same caveat; the first real run on a machine with `glab` settles it.

## Setup

1. Install `glab` and sign in as the owner with `glab auth login`; see [installing-agent-clis.md](installing-agent-clis.md). The token lives in glab's credential store. Never put a token in this repo, in `config/`, in an issue or in a commit.
2. In Settings (or `config/webapp_config.json`) set `gitlab_group`, the group or user namespace whose projects the Board reads (subgroups included), and `gitlab_host` for a self-hosted instance. `gitlab_host` rides glab's `GITLAB_HOST` environment variable; empty means glab's default context. With no group set the Board's issue columns stay dormant and show a Settings hint.
3. Check one real response against the fixtures: `glab api "groups/<group>/issues?state=opened&per_page=1"`. The client relies on `references.full`, `closed_at` and `web_url`. If the instance answers differently, fix the fixtures and the client together.

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

Both are used by the Board's file-an-issue flow and by anything that answers on an issue. Not yet verified live.

## Owner checks

The owner-check feature (shipped work to validate, steps owed before a start) is meant to use GitLab scoped labels. It is opt-in and **not yet live**: the label scheme and the helper that reads and clears the labels ship with fleet-config-lite. Until then the Board's owner-check card stays empty.

## Merging

Merging through `glab mr merge --when-pipeline-succeeds` is a planned port step (see [Status](../README.md#status)); the Board merges nothing today.

## Two remotes for this repo

This repo itself can live on both forges: GitHub `origin` plus a private GitLab project of the same name as a second remote.

```bash
git remote add gitlab <url-of-the-private-gitlab-project>
git push gitlab main
```

Push branches the same way (`git push gitlab <branch>`). Never force-push either remote. `origin` stays the default for fetch; `gitlab` is a mirror pushed to deliberately. Not yet verified live.

## Working from an Android Git client

Nothing in the launcher assumes every push comes from the PC: the Board reads GitLab, not local git. A phone can push too. With an Android Git client such as **MGit**:

1. In GitLab, as the owner, create a **personal access token** with scope `read_repository` and `write_repository`. Keep it in the client's credential store; never paste it into a repo file, an issue or a chat.
2. Clone over **HTTPS** with the GitLab project URL (`https://<host>/<group>/<project>.git`), using your GitLab username and the token as the password.
3. Branch, edit, commit and push to that branch from the client. Do not force-push.
4. On the PC, `git fetch` and pull the branch (or the merged `main`) before the next session works on it, so the local checkout the Board's ▶/⚡ buttons launch in matches GitLab.

The flow is not yet verified on a handset; the client's exact menus are its own.
