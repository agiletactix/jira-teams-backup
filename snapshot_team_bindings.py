#!/usr/bin/env python3
"""snapshot_team_bindings.py: READ-ONLY. Records which team every issue has.

Run this BEFORE you delete, rename or recreate any team.

Writes one JSON file mapping issue key -> team NAME. The name is the part that
survives: a deleted team's ID is gone for good, and a recreated team with the same
name gets a new ID. restore_team_bindings.py uses this file to point issues back at
the team with the same name.

This script only issues GET requests. It changes nothing in Jira.

It also records the team definitions: team ID, name, description, membership
setting (the REST API calls it teamType: OPEN or MEMBER_INVITE, not official vs
regular), state, whether membership is synced from an Atlassian group, and the
members (account ID plus display name). restore_team_bindings.py uses that to
reactivate or recreate missing teams and add missing members before it re-points
the issues. The saved team ID is what lets it try a reactivation first.

Needs the organization ID for the team definitions. Without one it records the
issue bindings only and says so. The REST API used here does not cover the team
hierarchy (parent teams) or the team type (regular vs official). Atlassian's
GraphQL Teams API does, and this script does not use it yet, so neither is captured.

Member lists come from POST .../members, which only reads (the body carries the
paging cursor). Nothing here writes.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

import common

SEARCH_PATH = "/rest/api/3/search/jql"
DEFAULT_JQL = '"Team[Team]" is not EMPTY'
DEFAULT_OUT_DIR = Path("snapshots")


def fetch_bindings(session, base_url: str, field_id: str, jql: str = DEFAULT_JQL) -> Dict[str, Dict[str, Optional[str]]]:
    """key -> {"team_name", "team_id"}. Paginated with nextPageToken."""
    out: Dict[str, Dict[str, Optional[str]]] = {}
    token: Optional[str] = None
    while True:
        params: Dict[str, Any] = {"jql": jql, "maxResults": 100, "fields": field_id}
        if token:
            params["nextPageToken"] = token
        data = common.get_json(session, f"{base_url}{SEARCH_PATH}", params=params)
        for issue in data.get("issues", []):
            team = common.team_value_from_issue(issue, field_id)
            out[issue["key"]] = {"team_name": team.get("name"), "team_id": team.get("id")}
        token = data.get("nextPageToken")
        if data.get("isLast", True) or not token:
            break
    return out


def fetch_team_definitions(session, base_url: str, org_id: str, site_id: str) -> List[Dict[str, Any]]:
    """One record per team: id, name, description, type, state, managed_by, members."""
    live = common.fetch_live_teams(session, org_id, site_id)
    users: Dict[str, Optional[Dict[str, Any]]] = {}
    out: List[Dict[str, Any]] = []
    for name in sorted(live, key=str):
        for e in live[name]:
            members = []
            for account_id in common.fetch_team_member_ids(session, org_id, site_id, e["teamId"]):
                if account_id not in users:
                    users[account_id] = common.fetch_user(session, base_url, account_id)
                u = users[account_id] or {}
                members.append({"accountId": account_id, "displayName": u.get("displayName")})
            ref = e.get("externalReference") or {}
            out.append({
                "id": e["teamId"],
                "name": e["displayName"],
                "description": e.get("description") or "",
                "type": e.get("teamType") or "OPEN",
                "state": e.get("state") or "ACTIVE",
                "managed_by": ref.get("source"),
                "members": members,
            })
    return out


def build_snapshot(found: Dict[str, Dict[str, Optional[str]]], *, base_url: str, field_id: str, jql: str, teams: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    bindings = {k: v["team_name"] for k, v in sorted(found.items())}
    counts = Counter(bindings.values())
    snap = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "site": base_url,
        "field_id": field_id,
        "jql": jql,
        "total_issues": len(bindings),
        "counts_by_team": dict(sorted(counts.items(), key=lambda kv: str(kv[0]))),
        "bindings": bindings,
    }
    if teams is not None:
        snap["teams"] = teams
    return snap


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=None)
    ap.add_argument("--jql", default=DEFAULT_JQL, help=f"which issues to record (default: {DEFAULT_JQL})")
    ap.add_argument("--out", default=None, help="output file (default: snapshots/team-bindings-<today>.json)")
    ap.add_argument("--skip-teams", action="store_true", help="record issue bindings only, not team definitions and members")
    args = ap.parse_args(argv)

    settings = common.load_settings(args.config)
    session = common.make_session(settings)
    field_id = common.resolve_team_field_id(session, settings)

    found = fetch_bindings(session, settings.base_url, field_id, args.jql)
    teams = None
    if args.skip_teams:
        pass
    elif not settings.org_id:
        common.eprint("note: no organization ID set, so team definitions and members were not recorded. Bindings only.")
    else:
        site_id = common.resolve_cloud_id(session, settings.base_url)
        teams = fetch_team_definitions(session, settings.base_url, settings.org_id, site_id)
    snapshot = build_snapshot(found, base_url=settings.base_url, field_id=field_id, jql=args.jql, teams=teams)

    out = Path(args.out) if args.out else DEFAULT_OUT_DIR / f"team-bindings-{date.today().isoformat()}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(snapshot, indent=2, sort_keys=False))

    print(f"{snapshot['total_issues']} issue(s) with a Team set -> {out}")
    if teams is not None:
        print(f"{len(teams)} team(s), {sum(len(t['members']) for t in teams)} membership(s) recorded")
    for name, count in snapshot["counts_by_team"].items():
        print(f"  {name}: {count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
