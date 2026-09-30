# jira-teams-backup

Delete a team in Jira and every issue that pointed at it shows "Unknown" in the Team field.

Atlassian gives you roughly 30 days to reactivate a deleted team from its profile page. That brings back the team name, description and members. If you rebuild teams on purpose, or miss the window, the team is gone and the issues stay orphaned. A recreated team with the same name gets a new ID, so nothing reconnects on its own. And there is no bulk edit for the Team field.

So the fix is to write down each team (name, description, members) and which issue belongs to which team **name** before you touch anything, then rebuild the teams and point the issues back afterward.

**Run the snapshot before any team change.** It is read-only and takes seconds.

## What is in here

| Script | What it does | Writes to Jira? |
|---|---|---|
| `snapshot_team_bindings.py` | Saves a JSON file: every team (name, description, type, members) and issue key -> team name | Never. Reads only. |
| `teams.py` | Lists teams. Creates the ones from your roster that are missing, matched by name | Only with `--apply` |
| `restore_team_bindings.py` | Recreates missing teams, adds missing members, then sets the Team field on issues | Only with `--apply` |

## Quick start

You need Python 3.9 or newer.

```
git clone https://github.com/agiletactix/jira-teams-backup.git
cd jira-teams-backup
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Set your settings as environment variables:

```
export JIRA_SITE=yoursite                  # or yoursite.atlassian.net
export JIRA_EMAIL=you@example.com
export JIRA_API_TOKEN=...                  # or JIRA_API_TOKEN_FILE=/path/to/file
export ATLASSIAN_ORG_ID=...
```

- **API token:** create one at id.atlassian.com under Security, API tokens. It never goes in a config file.
- **Org ID:** open Atlassian Administration at admin.atlassian.com. The ID is the long UUID in the address bar after `/o/`.
- **Config file (optional):** copy `teams.example.yaml` to `teams.yaml`. It holds your team roster and, if you like, the site, email and org ID. Environment variables win over the file. `teams.yaml` is gitignored.
- **Team field ID:** detected automatically. If detection fails, set `JIRA_TEAM_FIELD_ID` (usually `customfield_10001`).

## The workflow: dry run first, every time

Nothing writes to Jira unless you pass `--apply`. Read the plan, then apply.

**1. Snapshot, before anything else.**

```
python3 snapshot_team_bindings.py
```

Writes `snapshots/team-bindings-<date>.json` and prints the issue count per team, plus the team and member totals. Check the numbers look right. The `snapshots/` folder is gitignored, because it holds your issue keys and account IDs. Team definitions need the org ID; without it you get issue bindings only.

**2. Make your team change** (delete, reset, rebuild, whatever you are doing).

**3. Restore teams, members and issues.**

```
python3 restore_team_bindings.py            # dry run: per-team plan, then per-issue plan
python3 restore_team_bindings.py --apply
```

The teams phase runs first: it creates missing teams and adds missing members. Then the issues are re-pointed. If you only want the issues, pass `--bindings-only`. (`teams.py create` still works if you would rather build teams from a hand-written roster.)

Run the dry run again afterward. When everything is restored it reports every issue as already right.

## What each script does and does not do

**`snapshot_team_bindings.py`**
- Does: record issue key -> team name for every issue with a Team set (change the scope with `--jql`). Also record every team: name, description, type (open or closed), state, whether its membership is synced from a group, and its members (account ID and display name).
- Does not: record parent teams. The Teams API does not expose them. Pass `--skip-teams` for issue bindings only.

**`teams.py`**
- Does: create missing teams from your roster, skip ones that already exist by name, and add members you list under `members:` (or yourself with `--add-me`).
- Does not: delete or rename anything. Members are only ever added.

**`restore_team_bindings.py`**
- Does: create teams that are missing, by name, with the saved description and type. Add the saved members who are not on the team yet. Then set the Team field on issues in the snapshot. It re-reads everything first, so existing teams, existing members and correct issues are skipped and it is safe to re-run.
- Skips and reports, never forces:
  - Teams whose membership is synced from a group. The API will not take manual members for them. Reconnect the group in Atlassian Administration.
  - Members whose account is deactivated or no longer exists.
  - Teams that were archived when the snapshot was taken.
  - A name that matches more than one live team. Nothing is guessed.
- A team that already exists with different members gets the missing ones added. Nobody is removed.
- Does not: delete anything, remove members, or restore parent teams.
- Old snapshots with no team definitions still work. The teams phase is skipped and the issues are re-pointed as before.
- Does not touch issues that are not in the snapshot.

## Good to know

- Tested against a Jira Cloud site I own. Try the dry runs on your own site first.
- Member lists come from an endpoint that is a POST but only reads. Nothing writes without `--apply`.
- The Teams API is eventually consistent. A team you just created or deleted can take a few seconds to show up in `teams.py list`.
- Writes are throttled and retried on HTTP 429.
- Run the offline tests with `python3 tests/test_offline.py`. They never touch the network.

Claude wrote it once. It's a plain Python file you own, no subscription to run it.

<!-- KIT_LINK -->Get the next script: LINK_TBD

MIT licensed. Built by Danny Liu, 20 years in tech, at [AgileTactix](https://github.com/agiletactix).
