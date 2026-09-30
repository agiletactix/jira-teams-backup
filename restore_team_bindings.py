#!/usr/bin/env python3
"""restore_team_bindings.py: bring teams, members and issue bindings back.

Reads a snapshot from snapshot_team_bindings.py and runs two phases.

  Phase 1, teams (only if the snapshot has team definitions):
    - Creates any team whose name is not live, with the saved description and type.
    - Adds the saved members who are missing, by account ID.
  Phase 2, bindings:
    - Sets the Team field on every issue that does not already point at the team
      that currently has the saved name. Re-reads each issue first, so re-running
      only touches what is still wrong.

DRY RUN unless you pass --apply. The dry run makes read-only calls only.

Skipped and reported, never forced:
  - Teams whose membership is synced from an Atlassian group (the API will not
    take manual members). Reconnect the group in Atlassian Administration.
  - Members whose account is deactivated or gone.
  - Teams that were archived when the snapshot was taken.
  - A name that matches more than one live team. Nothing is guessed.

It never deletes or removes anything. A team that already exists with other
members gets the missing ones added and keeps everyone else. Parent teams are
not restored, because the Teams API does not expose them.

Older snapshots with no team definitions still work: phase 1 is skipped.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

import common
import teams as teams_mod

SEARCH_PATH = "/rest/api/3/search/jql"
CHUNK = 50


def latest_snapshot(directory: Path = Path("snapshots")) -> Optional[Path]:
    found = sorted(directory.glob("team-bindings-*.json"))
    return found[-1] if found else None


def resolve_names(live: Dict[str, List[Dict[str, Any]]], names: List[str]) -> Dict[str, Dict[str, Optional[str]]]:
    """name -> {"id": ..., "problem": None | "not_found" | "ambiguous"}"""
    out: Dict[str, Dict[str, Optional[str]]] = {}
    for name in names:
        matches = live.get(name) or []
        if not matches:
            out[name] = {"id": None, "problem": "team_not_found"}
        elif len(matches) > 1:
            out[name] = {"id": None, "problem": "team_ambiguous"}
        else:
            out[name] = {"id": matches[0].get("teamId"), "problem": None}
    return out


INACTIVE_REASONS = {"deactivated": "account is deactivated", "removed": "account not found"}
PLACEHOLDER_ID = "<new team>"


def plan_teams(snap_teams: List[Dict[str, Any]], live: Dict[str, List[Dict[str, Any]]],
               live_members: Dict[str, List[str]], user_status: Dict[str, Optional[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Pure. One row per snapshot team.

    action: create | reuse | ambiguous | archived
    members_skipped_reason: None, or why no members will be added (group-synced)
    to_add: [{accountId, displayName}], already: count, inactive: [{accountId, displayName, why}]
    live_members: teamId -> account IDs. user_status: accountId -> {"active"} or None (gone).
    """
    rows = []
    for t in snap_teams:
        name = t["name"]
        matches = live.get(name) or []
        row: Dict[str, Any] = {"name": name, "description": t.get("description") or name, "type": t.get("type") or "OPEN",
                               "team_id": None, "to_add": [], "already": 0, "inactive": [], "members_skipped_reason": None}
        if t.get("state", "ACTIVE") != "ACTIVE":
            rows.append({**row, "action": "archived"})
            continue
        if len(matches) > 1:
            rows.append({**row, "action": "ambiguous"})
            continue
        if matches:
            row["action"], row["team_id"] = "reuse", matches[0].get("teamId")
            managed = bool(matches[0].get("externalReference"))
        else:
            row["action"] = "create"
            managed = False
        if managed:
            row["members_skipped_reason"] = "membership is synced from a group on the live team"
        elif t.get("managed_by") and not matches:
            row["members_skipped_reason"] = "membership was synced from a group when saved; reconnect the group after the team is created"
        else:
            have = set(live_members.get(row["team_id"] or "", []))
            for m in t.get("members") or []:
                aid = m["accountId"]
                if aid in have:
                    row["already"] += 1
                    continue
                st = user_status.get(aid, {"active": True})
                if st is None:
                    row["inactive"].append({**m, "why": INACTIVE_REASONS["removed"]})
                elif not st.get("active", True):
                    row["inactive"].append({**m, "why": INACTIVE_REASONS["deactivated"]})
                else:
                    row["to_add"].append(m)
        rows.append(row)
    return rows


def print_team_plan(rows: List[Dict[str, Any]]) -> None:
    print("TEAMS PLAN")
    for r in rows:
        label = {"create": "CREATE", "reuse": "REUSE", "ambiguous": "SKIP", "archived": "SKIP"}[r["action"]]
        line = f"  {label:6}  {r['name']}"
        if r["action"] == "ambiguous":
            line += "  (more than one live team has this name)"
        elif r["action"] == "archived":
            line += "  (archived when saved)"
        elif r["members_skipped_reason"]:
            line += f"  members skipped: {r['members_skipped_reason']}"
        else:
            line += f"  {len(r['to_add'])} member(s) to add, {r['already']} already there"
        print(line)
        for m in r["inactive"]:
            print(f"            skip member {m.get('displayName') or m['accountId']}: {m['why']}")
    n_create = sum(1 for r in rows if r["action"] == "create")
    n_add = sum(len(r["to_add"]) for r in rows)
    print(f"\n{n_create} team(s) to create, {n_add} member(s) to add.")


def gather_team_state(session, base_url: str, org_id: str, site_id: str, snap_teams: List[Dict[str, Any]],
                      live: Dict[str, List[Dict[str, Any]]]):
    """Read-only lookups the plan needs: live members of matching teams and the
    status of each saved member who is not already on the team."""
    live_members: Dict[str, List[str]] = {}
    for t in snap_teams:
        matches = live.get(t["name"]) or []
        if len(matches) == 1 and not matches[0].get("externalReference"):
            tid = matches[0]["teamId"]
            live_members[tid] = common.fetch_team_member_ids(session, org_id, site_id, tid)
    user_status: Dict[str, Optional[Dict[str, Any]]] = {}
    for t in snap_teams:
        matches = live.get(t["name"]) or []
        have = set(live_members.get(matches[0]["teamId"], [])) if len(matches) == 1 else set()
        for m in t.get("members") or []:
            aid = m["accountId"]
            if aid not in have and aid not in user_status:
                user_status[aid] = common.fetch_user(session, base_url, aid)
    return live_members, user_status


def apply_team_plan(session, org_id: str, site_id: str, rows: List[Dict[str, Any]]) -> int:
    failed = 0
    for r in rows:
        if r["action"] not in ("create", "reuse"):
            continue
        name = r["name"]
        try:
            team_id = r["team_id"]
            if r["action"] == "create":
                team_id = teams_mod.create_team(session, org_id, site_id, name, r["description"], r["type"]).get("teamId")
                print(f"  [{name}] created, id={team_id}")
            if r["to_add"]:
                result = teams_mod.add_members(session, org_id, team_id, [m["accountId"] for m in r["to_add"]])
                errors = result.get("errors") or []
                print(f"  [{name}] added {len(r['to_add']) - len(errors)} member(s)")
                for e in errors:
                    failed += 1
                    common.eprint(f"  [{name}] member not added: {e}")
        except Exception as e:  # noqa: BLE001 - keep going, re-run is idempotent
            failed += 1
            common.eprint(f"  [{name}] FAILED: {e}")
    print(f"\nTeams phase done, {failed} failure(s).")
    return failed


def fetch_current(session, base_url: str, field_id: str, keys: List[str]) -> Dict[str, Dict[str, Any]]:
    """key -> current Team value. A key that is missing from the result is an issue
    that no longer exists (or that you can no longer see)."""
    out: Dict[str, Dict[str, Any]] = {}
    for i in range(0, len(keys), CHUNK):
        jql = "key in (" + ",".join(keys[i:i + CHUNK]) + ")"
        token: Optional[str] = None
        while True:
            params: Dict[str, Any] = {"jql": jql, "maxResults": 100, "fields": field_id}
            if token:
                params["nextPageToken"] = token
            data = common.get_json(session, f"{base_url}{SEARCH_PATH}", params=params)
            for issue in data.get("issues", []):
                team = common.team_value_from_issue(issue, field_id)
                out[issue["key"]] = {"team_id": team.get("id"), "team_name": team.get("name")}
            token = data.get("nextPageToken")
            if data.get("isLast", True) or not token:
                break
    return out


def build_plan(bindings: Dict[str, str], resolved: Dict[str, Dict[str, Optional[str]]], current: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Pure. status: match | update | team_not_found | team_ambiguous | issue_not_found"""
    rows = []
    for key, name in sorted(bindings.items()):
        target = resolved.get(name, {"id": None, "problem": "team_not_found"})
        cur = current.get(key)
        if target["problem"]:
            status = target["problem"]
        elif cur is None:
            status = "issue_not_found"
        elif cur.get("team_id") == target["id"]:
            status = "match"
        else:
            status = "update"
        rows.append({"key": key, "team_name": name, "target_id": target["id"], "status": status})
    return rows


def print_plan(rows: List[Dict[str, Any]]) -> None:
    by_team: Dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        by_team[r["team_name"]][r["status"]] += 1
    print("PLAN")
    for name in sorted(by_team, key=str):
        c = by_team[name]
        print(f"  {name}: {sum(c.values())} issue(s), {c['match']} already right, {c['update']} to update, "
              f"{c['team_not_found']} team missing, {c['team_ambiguous']} team name ambiguous, {c['issue_not_found']} issue missing")
    overall = Counter(r["status"] for r in rows)
    print(f"\n{len(rows)} issue(s) in the snapshot: {overall['match']} already right, {overall['update']} to update.")
    if overall["team_not_found"]:
        print("\nNo live team with these names. Recreate them first (teams.py create --apply):")
        for n in sorted({r["team_name"] for r in rows if r["status"] == "team_not_found"}, key=str):
            print(f"  - {n}")
    if overall["team_ambiguous"]:
        print("\nMore than one live team has these names, so nothing was guessed:")
        for n in sorted({r["team_name"] for r in rows if r["status"] == "team_ambiguous"}, key=str):
            print(f"  - {n}")
    if overall["issue_not_found"]:
        print(f"\n{overall['issue_not_found']} issue key(s) not found on the site (deleted, or not visible to this account).")


def apply_plan(session, base_url: str, field_id: str, rows: List[Dict[str, Any]], rate_limit: float, max_retries: int) -> int:
    failed = bound = 0
    for r in rows:
        if r["status"] != "update":
            continue
        try:
            common.request_with_retry(
                session, "PUT", f"{base_url}/rest/api/3/issue/{r['key']}",
                json={"fields": {field_id: r["target_id"]}}, max_retries=max_retries,
            )
            bound += 1
            print(f"  [{r['key']}] -> {r['team_name']}")
        except Exception as e:  # noqa: BLE001 - resumable: re-run picks up what is left
            failed += 1
            common.eprint(f"  [{r['key']}] FAILED: {e}")
        if rate_limit:
            time.sleep(rate_limit)
    print(f"\n{bound} updated, {failed} failed.")
    return failed


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=None)
    ap.add_argument("--snapshot", default=None, help="snapshot file (default: newest in snapshots/)")
    ap.add_argument("--apply", action="store_true", help="actually write (teams, members, Team field). Default is a dry run.")
    ap.add_argument("--bindings-only", action="store_true", help="skip the teams phase and only re-point issues")
    ap.add_argument("--rate-limit-seconds", type=float, default=0.2, help="pause between writes (default 0.2)")
    ap.add_argument("--max-retries", type=int, default=5, help="retries per call on HTTP 429")
    args = ap.parse_args(argv)

    path = Path(args.snapshot) if args.snapshot else latest_snapshot()
    if not path or not path.exists():
        common.eprint("No snapshot found. Run snapshot_team_bindings.py first, or pass --snapshot.")
        return 2
    snapshot = json.loads(path.read_text())
    bindings: Dict[str, str] = snapshot["bindings"]
    print(f"Loaded {len(bindings)} issue(s) from {path} (taken {snapshot.get('generated_at')}).")

    settings = common.load_settings(args.config)
    session = common.make_session(settings)
    org_id = settings.require_org_id()
    site_id = common.resolve_cloud_id(session, settings.base_url)
    field_id = common.resolve_team_field_id(session, settings)

    live = common.fetch_live_teams(session, org_id, site_id)

    snap_teams = None if args.bindings_only else snapshot.get("teams")
    if snap_teams is None:
        print("No team definitions in this snapshot (or --bindings-only). Skipping the teams phase.\n")
    else:
        live_members, user_status = gather_team_state(session, settings.base_url, org_id, site_id, snap_teams, live)
        team_rows = plan_teams(snap_teams, live, live_members, user_status)
        print_team_plan(team_rows)
        if args.apply:
            print("\nAPPLYING TEAMS")
            if apply_team_plan(session, org_id, site_id, team_rows):
                common.eprint("Some team steps failed. Fix them and re-run; finished steps are skipped. Bindings not attempted.")
                return 1
            live = common.fetch_live_teams(session, org_id, site_id)
        else:
            # Dry run: pretend the teams that would be created exist, so the bindings
            # plan shows what will happen after they do.
            for r in team_rows:
                if r["action"] == "create":
                    live = {**live, r["name"]: [{"teamId": PLACEHOLDER_ID}]}
        print()

    resolved = resolve_names(live, sorted({n for n in bindings.values() if n}, key=str))
    current = fetch_current(session, settings.base_url, field_id, sorted(bindings))
    rows = build_plan({k: v for k, v in bindings.items() if v}, resolved, current)
    print_plan(rows)

    if not args.apply:
        print("\nDry run. Nothing was written. Pass --apply to create teams, add members and set the Team field.")
        return 0
    if not any(r["status"] == "update" for r in rows):
        print("\nNothing to update.")
        return 0
    return 0 if apply_plan(session, settings.base_url, field_id, rows, args.rate_limit_seconds, args.max_retries) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
