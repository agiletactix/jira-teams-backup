"""Shared config loading and HTTP helpers for the jira-teams-backup scripts.

Settings come from an optional YAML file (default: teams.yaml) with environment
variables layered on top. Environment variables win.

  JIRA_SITE              yoursite, yoursite.atlassian.net, or the full https URL
  JIRA_EMAIL             the Atlassian account email that owns the API token
  JIRA_API_TOKEN         the API token (or use JIRA_API_TOKEN_FILE)
  JIRA_API_TOKEN_FILE    path to a file holding only the token
  ATLASSIAN_ORG_ID       the organization ID (see README for where to find it)
  JIRA_TEAM_FIELD_ID     optional, e.g. customfield_10001. Auto-detected if unset.

The token is never printed or logged.
"""
from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
import yaml

TEAMS_API_BASE = "https://api.atlassian.com/public/teams/v1/org"
DEFAULT_CONFIG_PATH = "teams.yaml"


def eprint(*args: Any) -> None:
    print(*args, file=sys.stderr)


def die(message: str) -> "None":
    eprint(f"error: {message}")
    raise SystemExit(2)


def normalize_site(value: str) -> str:
    """Accepts 'acme', 'acme.atlassian.net' or 'https://acme.atlassian.net/' and
    returns the base URL."""
    v = value.strip().rstrip("/")
    if v.startswith("http://"):
        v = "https://" + v[len("http://"):]
    if not v.startswith("https://"):
        v = "https://" + v
    host = v[len("https://"):]
    if "." not in host:
        v = f"https://{host}.atlassian.net"
    return v


@dataclass
class Settings:
    base_url: str
    email: str
    token: str = field(repr=False)
    org_id: Optional[str] = None
    team_field_id: Optional[str] = None
    roster: List[Dict[str, Any]] = field(default_factory=list)

    def require_org_id(self) -> str:
        if not self.org_id:
            die("no organization ID. Set ATLASSIAN_ORG_ID or org_id in the config file.")
        return self.org_id


def _read_token() -> Optional[str]:
    token = os.environ.get("JIRA_API_TOKEN")
    if token:
        return token.strip()
    path = os.environ.get("JIRA_API_TOKEN_FILE")
    if path:
        p = Path(path).expanduser()
        if not p.exists():
            die(f"JIRA_API_TOKEN_FILE points at {p}, which does not exist.")
        return p.read_text().strip()
    return None


def load_settings(config_path: Optional[str] = None) -> Settings:
    data: Dict[str, Any] = {}
    p = Path(config_path or DEFAULT_CONFIG_PATH).expanduser()
    if p.exists():
        data = yaml.safe_load(p.read_text()) or {}
    elif config_path:
        die(f"config file {p} not found.")

    site = os.environ.get("JIRA_SITE") or data.get("site")
    email = os.environ.get("JIRA_EMAIL") or data.get("email")
    token = _read_token()
    if not site:
        die("no site. Set JIRA_SITE or site in the config file.")
    if not email:
        die("no email. Set JIRA_EMAIL or email in the config file.")
    if not token:
        die("no API token. Set JIRA_API_TOKEN or JIRA_API_TOKEN_FILE. Tokens are never read from the config file.")

    return Settings(
        base_url=normalize_site(site),
        email=email,
        token=token,
        org_id=os.environ.get("ATLASSIAN_ORG_ID") or data.get("org_id"),
        team_field_id=os.environ.get("JIRA_TEAM_FIELD_ID") or data.get("team_field_id"),
        roster=list(data.get("teams") or []),
    )


def make_session(settings: Settings) -> requests.Session:
    s = requests.Session()
    s.auth = (settings.email, settings.token)
    s.headers.update({"Accept": "application/json"})
    return s


def _retry_delay(resp: requests.Response, attempt: int, base_delay: float) -> float:
    header = resp.headers.get("Retry-After")
    try:
        return float(header) if header else base_delay * (2 ** attempt)
    except ValueError:
        return base_delay * (2 ** attempt)


def request_with_retry(session: requests.Session, method: str, url: str, *, max_retries: int = 5, base_delay: float = 1.0, **kwargs: Any) -> requests.Response:
    """One HTTP call, retried with backoff on 429 only. Raises on any other error."""
    attempt = 0
    while True:
        resp = session.request(method, url, timeout=30, **kwargs)
        if resp.status_code == 429 and attempt < max_retries:
            delay = _retry_delay(resp, attempt, base_delay)
            eprint(f"  rate limited, retrying in {delay:.1f}s ({attempt + 1}/{max_retries})")
            time.sleep(delay)
            attempt += 1
            continue
        resp.raise_for_status()
        return resp


def get_json(session: requests.Session, url: str, **kwargs: Any) -> Any:
    return request_with_retry(session, "GET", url, **kwargs).json()


def resolve_cloud_id(session: requests.Session, base_url: str) -> str:
    data = get_json(session, f"{base_url}/_edge/tenant_info")
    cloud_id = data.get("cloudId")
    if not cloud_id:
        die(f"could not resolve the cloud ID for {base_url}")
    return cloud_id


def resolve_team_field_id(session: requests.Session, settings: Settings) -> str:
    """The system Team field is a custom field whose schema ends in ':atlassian-team'.
    Its id differs per site, so look it up unless the user pinned it."""
    if settings.team_field_id:
        return settings.team_field_id
    fields = get_json(session, f"{settings.base_url}/rest/api/3/field")
    for f in fields:
        custom = ((f.get("schema") or {}).get("custom")) or ""
        if custom.endswith(":atlassian-team"):
            return f["id"]
    die("could not find the Team field. Set JIRA_TEAM_FIELD_ID (for example customfield_10001).")
    return ""  # unreachable


def fetch_live_teams(session: requests.Session, org_id: str, site_id: str) -> Dict[str, List[Dict[str, Any]]]:
    """Read-only, paginated. Returns displayName -> list of team entities. A list,
    because nothing stops two teams from sharing a name; callers decide what that means."""
    out: Dict[str, List[Dict[str, Any]]] = {}
    cursor: Optional[str] = None
    while True:
        params: Dict[str, Any] = {"siteId": site_id}
        if cursor:
            params["cursor"] = cursor
        data = get_json(session, f"{TEAMS_API_BASE}/{org_id}/teams", params=params)
        for entity in data.get("entities", []):
            out.setdefault(entity["displayName"], []).append(entity)
        cursor = data.get("cursor")
        if not cursor:
            break
    return out


def fetch_team_member_ids(session: requests.Session, org_id: str, site_id: str, team_id: str) -> List[str]:
    """Account IDs of a team's members. The endpoint is a POST but it only reads:
    the body carries the paging cursor ('after') and page size ('first'). Paginated."""
    ids: List[str] = []
    after: Optional[str] = None
    while True:
        body: Dict[str, Any] = {"first": 50}
        if after:
            body["after"] = after
        data = request_with_retry(
            session, "POST", f"{TEAMS_API_BASE}/{org_id}/teams/{team_id}/members",
            params={"siteId": site_id}, json=body,
        ).json()
        ids.extend(m["accountId"] for m in data.get("results", []))
        page = data.get("pageInfo") or {}
        after = page.get("endCursor")
        if not page.get("hasNextPage") or not after:
            break
    return ids


def fetch_user(session: requests.Session, base_url: str, account_id: str) -> Optional[Dict[str, Any]]:
    """GET one Jira user. Returns {"displayName", "active"}, or None when the account
    is gone (404). Read-only."""
    resp = session.request("GET", f"{base_url}/rest/api/3/user", params={"accountId": account_id}, timeout=30)
    if resp.status_code in (400, 404):
        return None
    resp.raise_for_status()
    data = resp.json()
    return {"displayName": data.get("displayName"), "active": bool(data.get("active", True))}


def team_value_from_issue(issue: Dict[str, Any], field_id: str) -> Dict[str, Any]:
    """The Team field reads back as an object with id and name, or null."""
    return (issue.get("fields") or {}).get(field_id) or {}
