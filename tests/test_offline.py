"""Offline tests. No network: every HTTP call goes through a fake session."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import common  # noqa: E402
import restore_team_bindings as restore  # noqa: E402
import snapshot_team_bindings as snap  # noqa: E402
import teams  # noqa: E402


class FakeResponse:
    def __init__(self, data, status=200):
        self._data, self.status_code, self.headers = data, status, {}

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    """Routes by URL suffix. Records every non-GET call so tests can assert none happened."""

    def __init__(self, live_teams, issues):
        self.live_teams, self.issues, self.writes = live_teams, issues, []

    def request(self, method, url, **kw):
        if method != "GET":
            self.writes.append((method, url, kw.get("json")))
            return FakeResponse({"teamId": "new-id"})
        if url.endswith("/_edge/tenant_info"):
            return FakeResponse({"cloudId": "cloud-1"})
        if url.endswith("/rest/api/3/field"):
            return FakeResponse([{"id": "customfield_99", "schema": {"custom": "x:atlassian-team"}}])
        if url.endswith("/teams"):
            return FakeResponse({"entities": self.live_teams, "cursor": None})
        if url.endswith("/search/jql"):
            jql = kw["params"]["jql"]
            wanted = [i for i in self.issues if f"key in (" not in jql or i["key"] in jql]
            return FakeResponse({"issues": [{"key": i["key"], "fields": {"customfield_99": i["team"]}} for i in wanted], "isLast": True})
        raise AssertionError(url)


def env(**extra):
    base = {"JIRA_SITE": "example", "JIRA_EMAIL": "a@example.com", "JIRA_API_TOKEN": "t", "ATLASSIAN_ORG_ID": "org-1"}
    base.update(extra)
    return mock.patch.dict(os.environ, base, clear=True)


class TestConfig(unittest.TestCase):
    def test_site_forms(self):
        for v in ("example", "example.atlassian.net", "https://example.atlassian.net/"):
            self.assertEqual(common.normalize_site(v), "https://example.atlassian.net")

    def test_missing_token_is_fatal(self):
        with mock.patch.dict(os.environ, {"JIRA_SITE": "x", "JIRA_EMAIL": "a@b.c"}, clear=True):
            with self.assertRaises(SystemExit):
                common.load_settings()

    def test_token_not_in_repr(self):
        with env(JIRA_API_TOKEN="SECRET-VALUE-123"):
            self.assertNotIn("SECRET-VALUE-123", repr(common.load_settings()))


class TestTeams(unittest.TestCase):
    def roster_settings(self):
        with env():
            s = common.load_settings()
        s.roster = [{"name": "Alpha"}, {"name": "Bravo", "description": "b"}]
        return s

    def test_plan_reuses_by_name(self):
        rows = teams.build_plan(teams.load_roster(self.roster_settings()), {"Alpha": [{"teamId": "1"}]})
        self.assertEqual([r["action"] for r in rows], ["reuse", "create"])

    def test_dry_run_writes_nothing(self):
        s = self.roster_settings()
        fake = FakeSession([{"displayName": "Alpha", "teamId": "1"}], [])
        with mock.patch.object(common, "make_session", return_value=fake):
            self.assertEqual(teams.cmd_create(s, apply=False, only=None, add_me=False), 0)
        self.assertEqual(fake.writes, [])

    def test_apply_creates_only_missing(self):
        s = self.roster_settings()
        fake = FakeSession([{"displayName": "Alpha", "teamId": "1"}], [])
        with mock.patch.object(common, "make_session", return_value=fake):
            self.assertEqual(teams.cmd_create(s, apply=True, only=None, add_me=False), 0)
        self.assertEqual(len(fake.writes), 1)
        self.assertEqual(fake.writes[0][2]["displayName"], "Bravo")


class TestSnapshotRestore(unittest.TestCase):
    ISSUES = [
        {"key": "A-1", "team": {"id": "old-1", "name": "Alpha"}},
        {"key": "A-2", "team": {"id": "old-2", "name": "Bravo"}},
        {"key": "A-3", "team": {"id": "old-1", "name": "Alpha"}},
    ]

    def test_snapshot_is_name_keyed(self):
        fake = FakeSession([], self.ISSUES)
        found = snap.fetch_bindings(fake, "https://example.atlassian.net", "customfield_99")
        out = snap.build_snapshot(found, base_url="https://example.atlassian.net", field_id="customfield_99", jql=snap.DEFAULT_JQL)
        self.assertEqual(out["bindings"], {"A-1": "Alpha", "A-2": "Bravo", "A-3": "Alpha"})
        self.assertEqual(out["counts_by_team"], {"Alpha": 2, "Bravo": 1})
        self.assertEqual(fake.writes, [])

    def test_restore_plan_statuses(self):
        bindings = {"A-1": "Alpha", "A-2": "Bravo", "A-3": "Gone", "A-4": "Alpha"}
        live = {"Alpha": [{"teamId": "new-1"}], "Bravo": [{"teamId": "x"}, {"teamId": "y"}]}
        resolved = restore.resolve_names(live, ["Alpha", "Bravo", "Gone"])
        current = {"A-1": {"team_id": "old-1"}, "A-2": {"team_id": "old-2"}, "A-3": {"team_id": None}}
        status = {r["key"]: r["status"] for r in restore.build_plan(bindings, resolved, current)}
        self.assertEqual(status, {"A-1": "update", "A-2": "team_ambiguous", "A-3": "team_not_found", "A-4": "issue_not_found"})

    def test_restore_match_is_noop(self):
        rows = restore.build_plan({"A-1": "Alpha"}, {"Alpha": {"id": "n", "problem": None}}, {"A-1": {"team_id": "n"}})
        self.assertEqual(rows[0]["status"], "match")

    def test_restore_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d, env():
            p = Path(d) / "s.json"
            p.write_text(json.dumps({"bindings": {"A-1": "Alpha"}, "generated_at": "now"}))
            fake = FakeSession([{"displayName": "Alpha", "teamId": "new-1"}], self.ISSUES)
            with mock.patch.object(common, "make_session", return_value=fake):
                self.assertEqual(restore.main(["--snapshot", str(p)]), 0)
            self.assertEqual(fake.writes, [])
            with mock.patch.object(common, "make_session", return_value=fake):
                self.assertEqual(restore.main(["--snapshot", str(p), "--apply", "--rate-limit-seconds", "0"]), 0)
            self.assertEqual(len(fake.writes), 1)
            self.assertEqual(fake.writes[0][2], {"fields": {"customfield_99": "new-1"}})


if __name__ == "__main__":
    unittest.main()
