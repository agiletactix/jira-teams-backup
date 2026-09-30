# jira-teams-backup

Delete a team in Jira and every issue that pointed at it shows "Unknown" in the Team field.

Atlassian gives you roughly 30 days to reactivate a deleted team from its profile page. That brings back the team name, description and members. If you rebuild teams on purpose, or miss the window, the issues stay orphaned. A recreated team with the same name gets a new ID, so nothing reconnects on its own. And there is no bulk edit for the Team field.

So the fix is to write down which issue belongs to which team **name** before you touch anything, then point the issues back afterward.

**Run the snapshot before any team change.** It is read-only and takes seconds.

## What is in here

| Script | What it does | Writes to Jira? |
|---|---|---|
| `snapshot_team_bindings.py` | Saves a JSON file: issue key -> team name | Never. GET requests only. |
| `teams.py` | Lists teams. Creates the ones from your roster that are missing, matched by name | Only with `--apply` |
| `restore_team_bindings.py` | Sets the Team field on issues to the team that currently has the saved name | Only with `--apply` |

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

Writes `snapshots/team-bindings-<date>.json` and prints the count per team. Check the numbers look right. The `snapshots/` folder is gitignored, because it holds your issue keys.

**2. Make your team change** (delete, reset, rebuild, whatever you are doing).

**3. Recreate the teams, if you need to.**

```
python3 teams.py create            # dry run: shows what would be created
python3 teams.py create --apply    # creates the missing ones
```

Matching is by name, so running it twice creates nothing the second time.

**4. Point the issues back.**

```
python3 restore_team_bindings.py            # dry run: per-team plan
python3 restore_team_bindings.py --apply
```

Run the dry run again afterward. When everything is restored it reports every issue as already right.

## What each script does and does not do

**`snapshot_team_bindings.py`**
- Does: record issue key -> team name for every issue with a Team set. Change the scope with `--jql`.
- Does not: record team membership, descriptions or anything else about the teams themselves.

**`teams.py`**
- Does: create missing teams from your roster, skip ones that already exist by name, and add members you list under `members:` (or yourself with `--add-me`).
- Does not: delete or rename anything. Members are only ever added.
- New teams start with no members unless you tell it otherwise.

**`restore_team_bindings.py`**
- Does: set the Team field on issues in the snapshot. It re-reads each issue first, so it only touches what is still wrong and is safe to re-run.
- Does **not** restore team membership. Membership is not in the snapshot, and the script never adds or removes a member. If you need members back, list them in your roster and use `teams.py`, or use Atlassian's own reactivation inside the 30-day window.
- Does not create teams. A name with no live team is reported and skipped.
- Does not guess. If two live teams share a name, those issues are skipped and listed.
- Does not touch issues that are not in the snapshot.

## Good to know

- Tested against a Jira Cloud site I own. Try the dry runs on your own site first.
- The Teams API is eventually consistent. A team you just created or deleted can take a few seconds to show up in `teams.py list`.
- Writes are throttled and retried on HTTP 429.
- Run the offline tests with `python3 tests/test_offline.py`. They never touch the network.

Claude wrote it once. It's a plain Python file you own, no subscription to run it.

<!-- KIT_LINK -->Get the next script: LINK_TBD

MIT licensed. Built by Danny Liu, 20 years in tech, at [AgileTactix](https://github.com/agiletactix).
