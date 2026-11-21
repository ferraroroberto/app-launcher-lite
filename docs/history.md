# History

The **History** sheet answers "what did I ship this month?". It opens from the clock button in the Projects card on the Code tab, and shows lines of code added and removed, merge requests merged, and issues opened and closed, grouped by day, week or month over 30 days, 90 days, 6 months or a custom range. Three tiles (each with a chart) and a per-project table sortable by column sit under the controls; a Data sources card lists each source's age. There are no token or usage figures: Copilot's token history is not probed, so those tiles are left out rather than shown empty.

## Two sources

| Source | Figures | Read from |
| --- | --- | --- |
| Local git | lines added and removed | `git log <default branch> --numstat` in each project folder, through `src/git_utils.run_git` |
| GitLab | merge requests merged, issues opened, issues closed | `glab api`, three reads per project (see [GitLab](gitlab.md#what-the-history-sheet-reads)) |

The projects are the child directories of `projects_dir` (minus `projects_ignore`) that have a `.git` folder. A folder whose `.git` is a file is a linked worktree of another folder's repository and is left out, so its lines are not counted twice. A project's GitLab path comes from whichever of its remotes sits on the GitLab host (`gitlab_host`, else gitlab.com); a remote named `gitlab` wins over `origin`. The default branch is `origin/HEAD`, then `origin/main`, `origin/master`, `main`, `master`, as last fetched, so the Projects card's Refresh keeps the lines current. Days are the local calendar day for both sources.

## Cache first, refresh on request

`GET /api/progress` (`app/webapp/routers/progress.py`) answers from `webapp/progress-totals.json` and runs no `git` or `glab`, however old that file is. Opening the sheet, changing the grouping and changing the range are all cache reads. **Refresh** in the Data sources card is the only thing that re-reads: it starts one background run, a toast rides the sheet's footer, and the sheet polls the cache and updates in place when it ends. A second tap while one runs starts nothing.

A first run backfills 400 days, the longest range the sheet accepts. Every later run re-reads from 7 days before that source's stored cursor and replaces those days, so a commit pushed late is still counted and a repeat refresh never counts anything twice. The file is written after every project, so an interrupted backfill resumes where it stopped. It is gitignored runtime data.

## Honest states

A figure that was not measured never reads `0`.

| Where | Word | Meaning |
| --- | --- | --- |
| a source (Data sources card, tile, chart) | `unknown` | nothing of that source has been read yet |
| a source | `stale` | the last read failed for some project; its previous days are kept and drawn |
| a source | `unavailable` | the saved file could not be read |
| a project's GitLab cells | `skipped` | the folder has no remote on the GitLab host; it is listed under the GitLab source and its lines still count |
| a project's cells | `unknown` | that project's source has not been read yet |
| the GitLab source | a note naming projects | a read came back as a full page of 100, so its older days may be incomplete (the reads do not paginate) |

The two sources are aged and can fail separately: a GitLab outage leaves the line charts drawn.

## Limits

- Lines are counted from the default branch's commits by committer date, so a rebased or squashed commit counts once, on the day it was made on that branch. Binary files add no lines. Merge commits add none.
- Issues carrying the audit ledger label (`audit-meta`) are not work and are not counted.
- Merge requests merged and issues opened and closed are the project's, whoever made them.
