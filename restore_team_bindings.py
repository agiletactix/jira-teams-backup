#!/usr/bin/env python3
"""restore_team_bindings.py: point issues back at the team with the same NAME.

Reads a snapshot from snapshot_team_bindings.py, finds the team that currently has
each name, and sets the Team field on every issue that does not already match.

DRY RUN unless you pass --apply. The dry run makes read-only calls only.

What it does:
  - Sets the Team field on issues, and nothing else.
  - Re-reads each issue's current value first, so re-running is safe and only
    touches what is still wrong.

What it does not do:
  - It does NOT restore team membership. Members are not in the snapshot and this
    script never adds or removes a member. A recreated team comes back with
    whatever members you give it (see teams.py).
  - It does NOT create teams. A name with no live team is reported and skipped.
  - It does not touch issues that are not in the snapshot.
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
    ap.add_argument("--apply", action="store_true", help="actually set the Team field. Default is a dry run.")
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
    resolved = resolve_names(live, sorted({n for n in bindings.values() if n}, key=str))
    current = fetch_current(session, settings.base_url, field_id, sorted(bindings))
    rows = build_plan({k: v for k, v in bindings.items() if v}, resolved, current)
    print_plan(rows)

    if not args.apply:
        print("\nDry run. Nothing was written. Pass --apply to set the Team field.")
        return 0
    if not any(r["status"] == "update" for r in rows):
        print("\nNothing to update.")
        return 0
    return 0 if apply_plan(session, settings.base_url, field_id, rows, args.rate_limit_seconds, args.max_retries) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
