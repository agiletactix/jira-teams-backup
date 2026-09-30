#!/usr/bin/env python3
"""teams.py: create Atlassian Teams from a roster file, idempotent by name.

Commands:
  list                 Read-only. Lists the teams that exist right now.
  create [--apply]     Creates every team in the roster that does not already exist.
                       A team with the same name is reused, never duplicated.
                       DRY RUN unless you pass --apply.

Nothing is written without --apply. The dry run still makes read-only calls to
see which teams already exist.

Membership: a team you create starts empty. List account IDs under `members:` in
the roster, or pass --add-me to add the account that owns the API token. Members
are only ever added, never removed.

Settings: see common.py, or the README.
"""
from __future__ import annotations

import argparse
import sys
from typing import Any, Dict, List, Optional

import requests

import common
from common import TEAMS_API_BASE, Settings


def load_roster(settings: Settings, only: Optional[str] = None) -> List[Dict[str, Any]]:
    """Normalises the `teams:` block of the config file."""
    out = []
    for entry in settings.roster:
        if isinstance(entry, str):
            entry = {"name": entry}
        name = (entry.get("name") or "").strip()
        if not name:
            common.die("a roster entry has no name.")
        out.append({
            "name": name,
            "description": entry.get("description") or name,
            "members": list(entry.get("members") or []),
        })
    if only:
        out = [t for t in out if t["name"] == only]
        if not out:
            common.die(f"--only {only!r} matches nothing in the roster.")
    if not out:
        common.die("the roster is empty. Add a `teams:` block to your config file (see teams.example.yaml).")
    seen = set()
    for t in out:
        if t["name"] in seen:
            common.die(f"team {t['name']!r} appears twice in the roster.")
        seen.add(t["name"])
    return out


def build_plan(roster: List[Dict[str, Any]], live: Dict[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Pure: create or reuse, per team. Same rule the real run uses."""
    rows = []
    for team in roster:
        existing = live.get(team["name"]) or []
        if existing:
            rows.append({**team, "action": "reuse", "team_id": existing[0].get("teamId"), "duplicates": len(existing) > 1})
        else:
            rows.append({**team, "action": "create", "team_id": None, "duplicates": False})
    return rows


def print_plan(rows: List[Dict[str, Any]], apply: bool) -> None:
    print("PLAN" if not apply else "APPLYING")
    for r in rows:
        if r["action"] == "create":
            print(f"  CREATE  {r['name']}")
        else:
            note = "  (warning: more than one live team has this name)" if r["duplicates"] else ""
            print(f"  REUSE   {r['name']}  id={r['team_id']}{note}")
    n_create = sum(1 for r in rows if r["action"] == "create")
    print(f"\n{n_create} to create, {len(rows) - n_create} already exist.")


def create_team(session: requests.Session, org_id: str, site_id: str, name: str, description: str, team_type: str = "OPEN") -> Dict[str, Any]:
    payload = {"displayName": name, "description": description, "teamType": team_type, "siteId": site_id}
    resp = common.request_with_retry(session, "POST", f"{TEAMS_API_BASE}/{org_id}/teams", json=payload)
    return resp.json()


def add_members(session: requests.Session, org_id: str, team_id: str, account_ids: List[str]) -> Dict[str, Any]:
    """Add-only. The API answers 200 with per-account failures in `errors`, so callers
    should look at the returned body."""
    payload = {"members": [{"accountId": a} for a in account_ids]}
    resp = common.request_with_retry(session, "POST", f"{TEAMS_API_BASE}/{org_id}/teams/{team_id}/members/add", json=payload)
    try:
        return resp.json() or {}
    except ValueError:
        return {}


def cmd_list(settings: Settings) -> int:
    session = common.make_session(settings)
    org_id = settings.require_org_id()
    site_id = common.resolve_cloud_id(session, settings.base_url)
    live = common.fetch_live_teams(session, org_id, site_id)
    total = sum(len(v) for v in live.values())
    print(f"{total} team(s) on {settings.base_url}\n")
    for name, entities in sorted(live.items()):
        for e in entities:
            print(f"  {name}  id={e.get('teamId')}  state={e.get('state')}  type={e.get('teamType')}")
    return 0


def cmd_create(settings: Settings, apply: bool, only: Optional[str], add_me: bool) -> int:
    roster = load_roster(settings, only)
    session = common.make_session(settings)
    org_id = settings.require_org_id()
    site_id = common.resolve_cloud_id(session, settings.base_url)
    live = common.fetch_live_teams(session, org_id, site_id)

    rows = build_plan(roster, live)
    print_plan(rows, apply)
    if not apply:
        print("\nDry run. Nothing was written. Pass --apply to create these teams.")
        return 0

    me: Optional[str] = None
    if add_me:
        me = common.get_json(session, f"{settings.base_url}/rest/api/3/myself")["accountId"]

    created = reused = failed = 0
    for row in rows:
        name = row["name"]
        try:
            if row["action"] == "create":
                team_id = create_team(session, org_id, site_id, name, row["description"]).get("teamId")
                print(f"  [{name}] created, id={team_id}")
                created += 1
            else:
                team_id = row["team_id"]
                print(f"  [{name}] already exists, reusing")
                reused += 1
            members = list(row["members"]) + ([me] if me else [])
            if members:
                add_members(session, org_id, team_id, members)
                print(f"  [{name}] ensured {len(members)} member(s)")
        except Exception as e:  # noqa: BLE001 - keep going, report at the end
            common.eprint(f"  [{name}] FAILED: {e}")
            failed += 1
    print(f"\n{created} created, {reused} reused, {failed} failed.")
    return 0 if failed == 0 else 1


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=None, help=f"config file (default: {common.DEFAULT_CONFIG_PATH} if present)")
    sub = ap.add_subparsers(dest="command")
    sub.add_parser("list", help="read-only: list existing teams")
    p = sub.add_parser("create", help="create missing teams (dry run unless --apply)")
    p.add_argument("--apply", action="store_true", help="actually write")
    p.add_argument("--only", default=None, help="restrict to one team by name")
    p.add_argument("--add-me", action="store_true", help="also add the token owner's account to each team")
    args = ap.parse_args(argv)

    command = args.command or "create"
    settings = common.load_settings(args.config)
    if command == "list":
        return cmd_list(settings)
    return cmd_create(settings, getattr(args, "apply", False), getattr(args, "only", None), getattr(args, "add_me", False))


if __name__ == "__main__":
    sys.exit(main())
