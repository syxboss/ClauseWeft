"""Exercise local pairing, named ownership and compatibility at the HTTP boundary."""

from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from uuid import uuid4

from clauseweft import audit
from clauseweft.api.application import Application
from clauseweft.api.server import Handler


class PersonalPairingTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = directory.name
        self.app = Application(self.path)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.app = self.app
        self.bootstrap = uuid4().hex
        self.server.authorization = ("Bearer " + self.bootstrap).encode()
        thread = Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, token=None, body=None, headers=None):
        conn = HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            fields = {"Content-Type": "application/json", "Idempotency-Key": uuid4().hex}
            if token:
                fields["Authorization"] = "Bearer " + token
            fields.update(headers or {})
            conn.request(method, "/api/v1/" + path, json.dumps(body) if body is not None else None, fields)
            response = conn.getresponse()
            return response.status, json.load(response)
        finally:
            conn.close()

    def pair(self):
        return self.request("POST", "auth/pair", self.bootstrap, {})

    def test_pairing_concurrency_preferences_logout_and_reopen(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            sessions = list(pool.map(lambda _: self.pair(), range(2)))
        self.assertEqual([s for s, _ in sessions], [200, 200])
        session, other = [result for _, result in sessions]
        self.assertEqual(session["user"]["id"], other["user"]["id"])
        self.assertTrue(session["user"]["personal_owner"])
        self.assertNotEqual(session["token"], self.bootstrap)
        self.assertNotEqual(session["token"], other["token"])
        token = session["token"]
        status, projects = self.request("GET", "projects?workspace_id=default", token)
        self.assertEqual(status, 200)
        self.assertEqual([p["id"] for p in projects["items"]], ["default"])
        self.assertEqual(self.request("PATCH", "me/preferences/default", token, {"instructions": "中文答复"},
                                      {"If-Match": '"0"'})[0], 200)
        self.server.app = Application(self.path)
        self.assertEqual(self.request("GET", "me/preferences/default", token)[1]["instructions"], "中文答复")
        self.assertEqual(self.request("POST", "auth/pair", token, {})[0], 403)
        self.assertEqual(self.request("POST", "auth/logout", token, {})[0], 200)
        self.assertEqual(self.request("GET", "auth/me", token)[0], 401)
        self.assertEqual(self.request("GET", "auth/me", other["token"])[0], 200)
        self.assertEqual(self.pair()[1]["user"]["id"], session["user"]["id"])
        with self.app.store.transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM personal_installation").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM platform_members").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM audit_entries WHERE action='auth.paired'").fetchone()[0], 1)
            self.assertFalse(any(token in line for line in db.iterdump()))

    def test_pair_requires_exact_bootstrap_and_same_origin(self):
        for token in (None, "incorrect"):
            self.assertEqual(self.request("POST", "auth/pair", token, {})[0], 401)
        self.assertEqual(self.request("POST", "auth/pair", self.bootstrap, {}, {"Origin": "https://example.invalid"})[0], 403)
        self.assertEqual(self.request("POST", "auth/pair", self.bootstrap, {"username": "mallory"})[0], 422)
        with self.app.store.transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM platform_members").fetchone()[0], 0)

    def test_existing_team_and_scoped_members_are_preserved(self):
        with self.app.store.transaction() as db:
            viewer = self.app.platform.add_member(db, {
                "username": "viewer", "password": "fixture-password-123", "role": "viewer",
            })
        session = self.app.platform.login("viewer", "fixture-password-123")
        self.assertEqual(self.request("POST", "auth/pair", session["token"], {})[0], 403)
        self.assertEqual(self.pair()[0], 409)
        with self.app.store.transaction() as db:
            self.assertEqual(self.app.platform.visible_workspaces(db, viewer), [])
            self.assertEqual(db.execute("SELECT count(*) FROM personal_installation").fetchone()[0], 0)

    def test_pairing_respects_expiry_and_deactivation(self):
        status, session = self.pair()
        self.assertEqual(status, 200)
        mid = session["user"]["id"]
        with self.app.store.transaction() as db:
            db.execute("UPDATE platform_sessions SET expires=0 WHERE member_id=?", (mid,))
        self.assertEqual(self.request("GET", "auth/me", session["token"])[0], 401)
        with self.app.store.transaction() as db:
            self.app.platform.update_member(db, mid, {"role": "viewer"}, 1)
        restricted = self.pair()[1]
        self.assertTrue(restricted["user"]["personal_owner"])
        self.assertEqual(restricted["user"]["capabilities"], ["read"])
        self.assertEqual(self.request("GET", "members", restricted["token"])[0], 403)
        self.assertEqual(self.request("GET", "agent-jobs/options?project_id=default", restricted["token"])[0], 403)
        with self.app.store.transaction() as db:
            self.app.platform.update_member(db, mid, {"active": False}, 2)
        self.assertEqual(self.pair()[0], 401)
        with self.app.store.transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM platform_members").fetchone()[0], 1)

    def test_old_ledger_without_live_team_resources_prevents_takeover(self):
        with self.app.store.transaction() as db:
            audit.record(db, audit.SYSTEM, "resource.saved", "projects/legacy-only")
        self.assertEqual(self.pair()[0], 409)
        with self.app.store.transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM personal_installation").fetchone()[0], 0)

    def test_old_deployment_with_trusted_resources_keeps_its_configuration(self):
        from pathlib import Path
        with TemporaryDirectory() as directory:
            config = Path(directory) / "trusted.json"
            config.write_text(json.dumps({"repositories": {"legacy-repo": {"path": directory, "allowlist": ["file.txt"]}}}))
            self.server.app = Application(Path(directory) / "state", config)
            with self.server.app.store.transaction() as db:
                db.execute("DELETE FROM platform_initialization WHERE name='personal-install-seed-v1'")
            self.assertEqual(self.pair()[0], 409)

    def test_projects_created_with_bootstrap_also_prevent_takeover(self):
        with self.app.store.transaction() as db:
            project = self.app.platform.save(db, "projects", {"name": "已有项目"})
        self.assertEqual(self.pair()[0], 409)
        self.assertEqual(self.request("GET", "projects/" + project["id"], self.bootstrap)[0], 200)
