"""Offline tests. No network: every HTTP call goes through a fake session."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

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
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


class FakeSession:
    """Routes by URL suffix. Records every non-GET call so tests can assert none happened."""

    def __init__(self, live_teams, issues, members=None, users=None):
        self.live_teams, self.issues, self.writes = live_teams, issues, []
        self.members = members or {}   # teamId -> [accountId]
        self.users = users or {}       # accountId -> {"displayName", "active"}; absent = 404

    def request(self, method, url, **kw):
        if method == "POST" and url.endswith("/members"):  # list members: a read
            return FakeResponse({"results": [{"accountId": a} for a in self.members.get(url.split("/")[-2], [])],
                                 "pageInfo": {"hasNextPage": False, "endCursor": None}})
        if method == "GET" and url.endswith("/rest/api/3/user"):
            u = self.users.get(kw["params"]["accountId"])
            return FakeResponse(u or {}, 200 if u else 404)
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


def team(tid, name, **kw):
    return {"teamId": tid, "displayName": name, "description": name + " d", "teamType": "OPEN", "state": "ACTIVE",
            "externalReference": kw.get("ext"), }


USERS = {"u1": {"displayName": "Una", "active": True}, "u2": {"displayName": "Dev", "active": True},
         "u3": {"displayName": "Off", "active": False}}  # u9 is absent: removed


class TestMembers(unittest.TestCase):
    SNAP_TEAMS = [
        {"name": "Alpha", "description": "A", "type": "CLOSED", "state": "ACTIVE", "managed_by": None,
         "members": [{"accountId": "u1", "displayName": "Una"}, {"accountId": "u2", "displayName": "Dev"},
                     {"accountId": "u3", "displayName": "Off"}, {"accountId": "u9", "displayName": None}]},
        {"name": "Synced", "description": "S", "type": "OPEN", "state": "ACTIVE", "managed_by": "ATLASSIAN_GROUP",
         "members": [{"accountId": "u1", "displayName": "Una"}]},
    ]

    def snapshot_file(self, d, teams="default"):
        p = Path(d) / "s.json"
        body = {"bindings": {"A-1": "Alpha"}, "generated_at": "now"}
        if teams == "default":
            body["teams"] = self.SNAP_TEAMS
        p.write_text(json.dumps(body))
        return p

    def test_snapshot_records_members_readonly(self):
        fake = FakeSession([team("t1", "Alpha"), team("t2", "Synced", ext={"source": "ATLASSIAN_GROUP", "id": "g"})], [],
                           members={"t1": ["u1", "u9"], "t2": ["u1"]}, users=USERS)
        defs = snap.fetch_team_definitions(fake, "https://example.atlassian.net", "org-1", "cloud-1")
        by = {t["name"]: t for t in defs}
        self.assertEqual(by["Alpha"]["members"], [{"accountId": "u1", "displayName": "Una"}, {"accountId": "u9", "displayName": None}])
        self.assertEqual(by["Synced"]["managed_by"], "ATLASSIAN_GROUP")
        self.assertEqual(by["Alpha"]["type"], "OPEN")
        self.assertEqual(fake.writes, [])
        out = snap.build_snapshot({}, base_url="x", field_id="f", jql="j", teams=defs)
        self.assertIn("teams", out)
        self.assertNotIn("teams", snap.build_snapshot({}, base_url="x", field_id="f", jql="j"))

    def plan(self, live, members):
        fake = FakeSession(live, [], members=members, users=USERS)
        by = {}
        for t in live:
            by.setdefault(t["displayName"], []).append(t)
        lm, us = restore.gather_team_state(fake, "https://example.atlassian.net", "org-1", "cloud-1", self.SNAP_TEAMS, by)
        return restore.plan_teams(self.SNAP_TEAMS, by, lm, us), fake

    def test_plan_create_skips_inactive_and_group_synced(self):
        rows, fake = self.plan([], {})
        alpha, synced = rows
        self.assertEqual(alpha["action"], "create")
        self.assertEqual([m["accountId"] for m in alpha["to_add"]], ["u1", "u2"])
        self.assertEqual({m["accountId"]: m["why"] for m in alpha["inactive"]},
                         {"u3": "account is deactivated", "u9": "account not found"})
        self.assertEqual(synced["action"], "create")
        self.assertEqual(synced["to_add"], [])
        self.assertTrue(synced["members_skipped_reason"])
        self.assertEqual(fake.writes, [])

    def test_existing_team_adds_only_missing_never_removes(self):
        rows, _ = self.plan([team("t1", "Alpha"), team("t2", "Synced")], {"t1": ["u1", "zz"], "t2": []})
        self.assertEqual(rows[0]["action"], "reuse")
        self.assertEqual([m["accountId"] for m in rows[0]["to_add"]], ["u2"])
        self.assertEqual(rows[0]["already"], 1)

    def test_live_verified_team_skipped(self):
        rows, _ = self.plan([team("t1", "Alpha"), team("t2", "Synced", ext={"source": "ATLASSIAN_GROUP"})], {"t1": []})
        self.assertEqual(rows[1]["action"], "reuse")
        self.assertEqual(rows[1]["to_add"], [])
        self.assertIn("synced", rows[1]["members_skipped_reason"])

    def test_dry_run_plans_but_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d, env():
            p = self.snapshot_file(d)
            fake = FakeSession([], [{"key": "A-1", "team": None}], users=USERS)
            with mock.patch.object(common, "make_session", return_value=fake):
                self.assertEqual(restore.main(["--snapshot", str(p)]), 0)
            self.assertEqual(fake.writes, [])

    def test_apply_creates_adds_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d, env():
            p = self.snapshot_file(d)
            fake = FakeSession([], [{"key": "A-1", "team": None}], users=USERS)

            def request(method, url, **kw):
                if method == "POST" and url.endswith("/teams"):
                    fake.writes.append((method, url, kw["json"]))
                    fake.live_teams.append(team("new-a", kw["json"]["displayName"]))
                    return FakeResponse({"teamId": "new-a"}, 201)
                if method == "POST" and url.endswith("/members/add"):
                    fake.writes.append((method, url, kw["json"]))
                    fake.members.setdefault(url.split("/")[-3], []).extend(m["accountId"] for m in kw["json"]["members"])
                    return FakeResponse({"members": kw["json"]["members"], "errors": []})
                return orig(method, url, **kw)
            orig = fake.request
            fake.request = request
            args = ["--snapshot", str(p), "--apply", "--rate-limit-seconds", "0"]
            with mock.patch.object(common, "make_session", return_value=fake):
                self.assertEqual(restore.main(args), 0)
            kinds = [(w[0], w[1].rsplit("/", 1)[-1]) for w in fake.writes]
            self.assertEqual(kinds.count(("POST", "teams")), 2)  # Alpha and Synced
            adds = [w for w in fake.writes if w[1].endswith("/members/add")]
            self.assertEqual([m["accountId"] for m in adds[0][2]["members"]], ["u1", "u2"])
            self.assertEqual(len(adds), 1)  # Synced got none
            created = [w[2] for w in fake.writes if w[1].endswith("/teams")]
            self.assertEqual(created[0]["teamType"], "CLOSED")
            self.assertFalse(any(w[0] in ("DELETE", "PATCH") or w[1].endswith("/members/remove") for w in fake.writes))
            n = len(fake.writes)
            # second run: teams exist now, members present. Only the issue binding may still be written.
            with mock.patch.object(common, "make_session", return_value=fake):
                self.assertEqual(restore.main(args), 0)
            self.assertFalse([w for w in fake.writes[n:] if w[0] == "POST"])

    def test_old_format_snapshot_still_restores_bindings(self):
        with tempfile.TemporaryDirectory() as d, env():
            p = self.snapshot_file(d, teams=None)
            fake = FakeSession([team("new-1", "Alpha")], TestSnapshotRestore.ISSUES)
            with mock.patch.object(common, "make_session", return_value=fake):
                self.assertEqual(restore.main(["--snapshot", str(p), "--apply", "--rate-limit-seconds", "0"]), 0)
            self.assertEqual(len(fake.writes), 1)
            self.assertEqual(fake.writes[0][2], {"fields": {"customfield_99": "new-1"}})


class TestReactivate(unittest.TestCase):
    SNAP = [{"id": "old-a", "name": "Alpha", "description": "A", "type": "OPEN", "state": "ACTIVE", "managed_by": None,
             "members": [{"accountId": "u1", "displayName": "Una"}]}]

    def setup_fake(self, restore_status):
        fake = FakeSession([], [], users=USERS)
        orig = fake.request

        def request(method, url, **kw):
            if method == "POST" and url.endswith("/restore"):
                fake.writes.append((method, url, None))
                return FakeResponse({}, restore_status)
            if method == "POST" and url.endswith("/teams"):
                fake.writes.append((method, url, kw["json"]))
                fake.live_teams.append(team("new-a", "Alpha"))
                return FakeResponse({"teamId": "new-a"}, 201)
            if method == "POST" and url.endswith("/members/add"):
                fake.writes.append((method, url, kw["json"]))
                return FakeResponse({"errors": []})
            return orig(method, url, **kw)
        fake.request = request
        return fake

    def rows(self, fake):
        lm, us = restore.gather_team_state(fake, "https://example.atlassian.net", "org-1", "cloud-1", self.SNAP, {})
        return restore.plan_teams(self.SNAP, {}, lm, us)

    def test_plan_marks_reactivate_only_with_saved_id(self):
        fake = self.setup_fake(204)
        self.assertEqual(self.rows(fake)[0]["action"], "reactivate")
        no_id = [{k: v for k, v in self.SNAP[0].items() if k != "id"}]
        self.assertEqual(restore.plan_teams(no_id, {}, {}, {})[0]["action"], "create")

    def test_dry_run_says_would_reactivate_and_writes_nothing(self):
        fake = self.setup_fake(204)
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            restore.print_team_plan(self.rows(fake))
        self.assertIn("would reactivate", buf.getvalue())
        self.assertIn("keeps ID and links", buf.getvalue())
        self.assertEqual(fake.writes, [])

    def test_apply_reactivates_by_saved_id_without_creating(self):
        fake = self.setup_fake(204)
        failed = restore.apply_team_plan(fake, "org-1", "cloud-1", self.rows(fake))
        self.assertEqual(failed, 0)
        urls = [w[1] for w in fake.writes]
        self.assertTrue(urls[0].endswith("/teams/old-a/restore"))
        self.assertFalse(any(u.endswith("/teams") for u in urls))
        self.assertTrue(any(u.endswith("/teams/old-a/members/add") for u in urls))

    def test_apply_falls_back_to_create_when_not_restorable(self):
        fake = self.setup_fake(404)
        failed = restore.apply_team_plan(fake, "org-1", "cloud-1", self.rows(fake))
        self.assertEqual(failed, 0)
        urls = [w[1] for w in fake.writes]
        self.assertTrue(urls[0].endswith("/restore"))
        self.assertTrue(any(u.endswith("/teams") for u in urls))

    def test_other_errors_are_not_swallowed(self):
        fake = self.setup_fake(500)
        failed = restore.apply_team_plan(fake, "org-1", "cloud-1", self.rows(fake))
        self.assertEqual(failed, 1)
        self.assertFalse(any(w[1].endswith("/teams") for w in fake.writes))

    def test_snapshot_records_team_id(self):
        fake = FakeSession([team("t1", "Alpha")], [], members={"t1": []}, users=USERS)
        defs = snap.fetch_team_definitions(fake, "https://example.atlassian.net", "org-1", "cloud-1")
        self.assertEqual(defs[0]["id"], "t1")


if __name__ == "__main__":
    unittest.main()
