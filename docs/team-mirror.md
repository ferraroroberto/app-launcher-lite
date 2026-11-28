# Team Mirror

Team Mirror publishes an app from its personal repo to a team repo as one clean snapshot commit, whenever the owner presses Publish. Ten personal commits in a day become one team commit. The team repo never sees a personal commit, name or address, and nothing flows back from the team repo.

## What a publish does

1. Lite fetches the personal main and the team main into a scratch repo it owns under the runtime data root. The live app folder is only read: a fetch from it writes nothing there. The personal main is the remote-tracking `origin/main` when there is one, so an unpushed local commit is never published.
2. It lays out the personal main's tree, removes the link's leave-out paths, and writes that tree. Files git ignores (`.env`, local config, the venv) are never in a commit tree, so they cannot be published.
3. The privacy scan reads every file of that tree, plus the PR title and text. A match stops the publish and names the file, line and rule, never the matched text.
4. If the team main has commits this computer did not publish, the publish stops and lists them until the owner confirms a second time, because publishing makes the team copy identical to the personal main again. A first publish onto a seeded team repo asks the same way.
5. Lite commits the tree with the team main as its only parent, author and committer both this computer's global git identity, and pushes it. A push is never forced, so a team main that moved in between refuses the push.
6. It records the published personal commit and the resulting team commit in `team-mirror-state.json` in the runtime data root, so the next PR text covers only what changed since and the next team-side check knows where the mirror left off.

## Where it publishes

The team URL's host picks the route:

| Team URL | Publish and merge | Open PR only |
|---|---|---|
| github.com | `gh pr create`, then `gh pr merge --squash` | `gh pr create` |
| any other https or ssh host | `glab mr create`, then `glab mr merge --squash` | `glab mr create` |
| a local path or `file://` | pushes straight to the team branch | pushes a `mirror/<date-time>` branch |

Sign-in is git's credential manager and the forge CLI's own; Lite stores no password or token. When the host refuses the merge (a protected branch, a required review), the result is "PR opened" with its link, and nothing is recorded until a later publish merges.

## The privacy scan

The rules are the personal-data ratchet's, from the vendored `scripts/personal_data_ratchet.py`: absolute user paths, personal email addresses, the owner's handle used as a value, and every word in the ratchet's machine-local term list. The webapp config's `mirror_scan_terms` adds words on top. The handle comes from the personal repo's GitHub `origin` or the term list's `handles`. A rule with nothing to read, such as a missing term list, makes the scan `unknown`, and `unknown` blocks a publish just as a match does. A line can keep an exception with the ratchet's own `<rule>-exempt: <reason>` marker.

## Status

Each app under the projects folder is Not linked, Up to date, N commits behind, or Blocked. Up to date means the snapshot tree equals the team main's tree, worked out fresh, so neither repo carries any bookkeeping. N counts personal commits since the last recorded publish; before the first publish the count is not known and the page says so. Blocked means the scan matched or could not run. A row is reused for 30 minutes unless the page asks for a refresh.

## Where state lives

- Links (`team_mirrors`: team URL, branch, leave-out paths) and `mirror_scan_terms`: the machine-local `config/webapp_config.json`, never committed.
- The last publish per app: `team-mirror-state.json` in the runtime data root.
- The scratch repos: `team-mirror/<app>` in the runtime data root, safe to delete; the next check rebuilds them.

## API

| Route | Gate | What it does |
|---|---|---|
| `GET /api/team-mirror` | token | every app's row and the work identity; `?refresh=1` skips the cache |
| `PUT /api/team-mirror/links/{app}` | passkey | saves `team_url`, `branch`, `leave_out` |
| `DELETE /api/team-mirror/links/{app}` | passkey | removes the link |
| `POST /api/team-mirror/test-access` | passkey | a read-only `ls-remote`, with a distinct message for a wrong URL, a refused sign-in, an unreachable host or a missing branch |
| `POST /api/team-mirror/{app}/preview` | passkey | the changed files, scan result, team-side commits and drafted PR text, writing nothing |
| `POST /api/team-mirror/{app}/publish` | passkey | `mode` (`merge` or `pr_only`), `confirm_team_changes`, an optional edited `pr_body`; every check runs again here |

The PR text is drafted by the local LLM hub from the commit subjects when `llm_hub_url` is set, and falls back to the plain list of subjects when it is not set or does not answer.
