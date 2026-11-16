"""C-19-8K part 1 : the key and project-mode API, end to end through the real storage/accounts.py functions.

The routes (backend/routers/keys.py) run in a FastAPI test client with a signed-in session ; storage.accounts runs
unchanged against the fake database of tests/test_storage_accounts.py (extended here with the two list queries).
Checked : the raw key is in the issue response once (never in the list, the DB, the audit events or a later
response), only its SHA-256 is stored, a revoked key fails verify_api_key, someone else's project or key is 404, the
read-only demo project is 403, CSRF on every write, the mode is saved, no-store on the issue response. Offline.
"""
from datetime import datetime, timezone
import hashlib
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.auth import service
from backend.routers.keys import router as keys_router
from backend.routers.projects import router as projects_router
from storage import accounts
from tests.test_storage_accounts import FakeConnection, FakeCursor

NOW = datetime(2026, 11, 17, 3, 0, tzinfo=timezone.utc)


class KeysFakeCursor(FakeCursor):
    """Adds list_projects / list_api_keys, and real timestamps (the API returns datetimes)."""

    def execute(self, sql, params=()):
        q = " ".join(sql.split())
        if "FROM projects WHERE owner_user_id = %s OR (owner_user_id IS NULL AND read_only = TRUE)" in q:
            self.state.setdefault("executed_sql", []).append(q)
            user_id = params[0]
            columns = ["id", "name", "mode", "rule_id", "provider", "upstream_url", "owner_user_id", "read_only",
                       "created_at"]
            rows = [tuple(p.get(c, "kmu") for c in columns) for p in sorted(self.state["projects"].values(),
                                                                             key=lambda p: (not p["read_only"], p["id"]))
                    if p["owner_user_id"] == user_id or (p["owner_user_id"] is None and p["read_only"])]
            self._set_rows(columns, rows)
            return
        if "FROM api_keys WHERE project_id = %s ORDER BY created_at DESC, id DESC" in q:
            self.state.setdefault("executed_sql", []).append(q)
            columns = ["id", "project_id", "key_prefix", "name", "created_at", "last_used_at", "revoked_at", "revoked"]
            keys = sorted((k for k in self.state["api_keys"].values() if k["project_id"] == params[0]),
                          key=lambda k: k["id"], reverse=True)
            self._set_rows(columns, [(k["id"], k["project_id"], k["key_prefix"], k["name"], k["created_at"],
                                      k["last_used_at"], k["revoked_at"], k["revoked_at"] is not None) for k in keys])
            return
        super().execute(sql, params)
        # The base fake writes placeholder strings ; make them timestamps.
        for key in self.state["api_keys"].values():
            for field in ("created_at", "last_used_at", "revoked_at"):
                if isinstance(key[field], str):
                    key[field] = NOW
        if q.startswith("INSERT INTO api_keys"):
            self._rows = [tuple(NOW if v == "created" else v for v in row) for row in self._rows]


class KeysFakeConnection(FakeConnection):
    def cursor(self):
        return KeysFakeCursor(self.state)


def project(pid, owner, name, read_only=False):
    return {"id": pid, "name": name, "mode": "observe", "rule_id": "deadlock-v0.3", "provider": "kmu",
            "upstream_url": "https://gateway.example.test/v1", "owner_user_id": owner, "read_only": read_only,
            "created_at": NOW}


class KeysApiTests(unittest.TestCase):
    def setUp(self):
        self.state = {
            "next_key_id": 1,
            "users": {7: {"id": 7, "account_status": "active"}, 8: {"id": 8, "account_status": "active"}},
            "projects": {1: project(1, None, "데모", read_only=True), 10: project(10, 7, "mine"),
                         20: project(20, 8, "theirs")},
            "api_keys": {}, "auth_events": [], "executed_sql": [], "due_user_ids": [],
        }
        db = patch("storage.accounts.get_connection", side_effect=lambda: KeysFakeConnection(self.state))
        db.start()
        self.addCleanup(db.stop)
        state = patch("backend.auth.service.fetch_account_state", return_value={
            "account_status": "active", "can_restore": None, "deletion_requested_at": None, "deletion_due_at": None})
        self.account_state = state.start()
        self.addCleanup(state.stop)
        app = FastAPI()
        app.include_router(projects_router)
        app.include_router(keys_router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.addCleanup(service._sessions.clear)
        self.csrf = self.sign_in(7)

    def sign_in(self, user_id):
        token = service.create_session({"id": user_id, "sub": f"google-{user_id}"})
        self.client.cookies.set("guardrail_session", token)
        return service.get_user(token)["csrf_token"]

    def issue(self, project_id=10, name="ci runner", csrf=None):
        return self.client.post(f"/api/projects/{project_id}/keys",
                                json={"name": name, "csrf_token": csrf or self.csrf})

    def test_raw_key_once_and_only_its_hash_stored(self):
        response = self.issue()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.headers["cache-control"], "no-store")
        body = response.json()
        raw = body["api_key"]
        self.assertRegex(raw, r"^gk_[0-9a-f]{8}_[A-Za-z0-9_-]{43,}$")
        self.assertEqual(body["key_prefix"], raw.split("_")[1])
        self.assertEqual((body["project_id"], body["name"], body["revoked"]), (10, "ci runner", False))
        self.assertNotIn("key_hash", body)
        stored = self.state["api_keys"][body["id"]]
        self.assertEqual(stored["key_hash"], hashlib.sha256(raw.encode()).hexdigest())
        self.assertNotIn(raw, repr(self.state))                    # not in the DB, the audit events or any SQL
        listing = self.client.get("/api/projects/10/keys")
        self.assertEqual(listing.status_code, 200)
        self.assertEqual([k["id"] for k in listing.json()], [body["id"]])
        self.assertNotIn(raw, listing.text)
        self.assertNotIn(stored["key_hash"], listing.text)
        self.assertNotIn("api_key", listing.json()[0])
        self.assertEqual(accounts.verify_api_key(raw)["id"], 10)  # the issued key works
        self.assertNotIn(raw, self.issue().text)                   # a second issue is a different key

    def test_revoked_key_fails_verify(self):
        raw = self.issue().json()["api_key"]
        key_id = self.issue().json()["id"] - 1
        response = self.client.post(f"/api/keys/{key_id}/revoke", json={"csrf_token": self.csrf})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"id": key_id, "revoked": True})
        self.assertIsNone(accounts.verify_api_key(raw))
        listed = {k["id"]: k for k in self.client.get("/api/projects/10/keys").json()}
        self.assertTrue(listed[key_id]["revoked"])
        self.assertIsNotNone(listed[key_id]["revoked_at"])
        self.assertFalse(listed[key_id + 1]["revoked"])
        self.assertIn(("api_key_revoked", 7), [e[:2] for e in self.state["auth_events"]])

    def test_someone_elses_project_and_key_are_404(self):
        theirs = self.issue(20)
        self.assertEqual(theirs.status_code, 404)
        self.assertEqual(self.client.get("/api/projects/20/keys").status_code, 404)
        self.assertEqual(self.client.get("/api/projects/999/keys").status_code, 404)
        self.assertEqual(self.client.patch("/api/projects/20", json={"mode": "block", "csrf_token": self.csrf})
                         .status_code, 404)
        self.assertEqual(self.state["projects"][20]["mode"], "observe")
        my_key = self.issue().json()["id"]
        other_csrf = self.sign_in(8)
        self.assertEqual(self.client.get("/api/projects/10/keys").status_code, 404)
        self.assertEqual(self.client.post(f"/api/keys/{my_key}/revoke", json={"csrf_token": other_csrf})
                         .status_code, 404)
        self.assertIsNone(self.state["api_keys"][my_key]["revoked_at"])
        self.assertEqual(self.client.post("/api/keys/999/revoke", json={"csrf_token": other_csrf}).status_code, 404)

    def test_demo_project_is_read_only_403(self):
        self.assertEqual(self.issue(1).status_code, 403)
        self.assertEqual(self.client.get("/api/projects/1/keys").status_code, 403)
        self.assertEqual(self.client.patch("/api/projects/1", json={"mode": "block", "csrf_token": self.csrf})
                         .status_code, 403)
        self.assertEqual(self.state["projects"][1]["mode"], "observe")
        self.assertEqual(self.state["api_keys"], {})

    def test_csrf_on_every_write(self):
        self.assertEqual(self.issue(csrf="wrong").status_code, 403)
        self.assertEqual(self.client.post("/api/projects/10/keys", json={"name": "x"}).status_code, 422)
        self.assertEqual(self.state["api_keys"], {})
        key_id = self.issue().json()["id"]
        self.assertEqual(self.client.post(f"/api/keys/{key_id}/revoke", json={"csrf_token": "wrong"}).status_code, 403)
        self.assertEqual(self.client.post(f"/api/keys/{key_id}/revoke", json={}).status_code, 422)
        self.assertIsNone(self.state["api_keys"][key_id]["revoked_at"])
        self.assertEqual(self.client.patch("/api/projects/10", json={"mode": "block", "csrf_token": "wrong"})
                         .status_code, 403)
        self.assertEqual(self.state["projects"][10]["mode"], "observe")

    def test_mode_is_saved(self):
        response = self.client.patch("/api/projects/10", json={"mode": "block", "csrf_token": self.csrf})
        self.assertEqual(response.status_code, 200)
        self.assertEqual((response.json()["id"], response.json()["mode"]), (10, "block"))
        self.assertNotIn("upstream_url", response.json())
        self.assertEqual(self.state["projects"][10]["mode"], "block")
        self.assertIn(("project_mode_changed", 7, {"project_id": 10, "mode": "block"}),
                      [(e[0], e[1], getattr(e[2], "obj", e[2])) for e in self.state["auth_events"]])
        for bad in ({"mode": "warn", "csrf_token": self.csrf}, {"mode": "block"},
                    {"mode": "block", "csrf_token": self.csrf, "rule_id": "x"}):
            self.assertEqual(self.client.patch("/api/projects/10", json=bad).status_code, 422)

    def test_invalid_names_and_ids(self):
        for name in ("   ", "x" * 101, "gsk_" + "A" * 30, 12):
            self.assertEqual(self.issue(name=name).status_code, 422, name)
        self.assertEqual(self.state["api_keys"], {})
        for path in ("/api/projects/0/keys", "/api/projects/-1/keys", f"/api/projects/{2**63}/keys"):
            self.assertEqual(self.client.get(path).status_code, 422, path)
        self.assertEqual(self.client.post(f"/api/keys/{2**63}/revoke", json={"csrf_token": self.csrf}).status_code, 422)

    def test_signed_out_and_recovery_sessions(self):
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/api/projects/10/keys").status_code, 401)
        self.assertEqual(self.issue().status_code, 401)
        self.assertEqual(self.client.post("/api/keys/1/revoke", json={"csrf_token": "x"}).status_code, 401)
        self.assertEqual(self.client.patch("/api/projects/10", json={"mode": "block", "csrf_token": "x"})
                         .status_code, 401)
        self.csrf = self.sign_in(7)
        self.account_state.return_value = {"account_status": "pending_deletion", "can_restore": True,
                                           "deletion_requested_at": None, "deletion_due_at": None}
        self.assertEqual(self.issue().status_code, 403)
        self.assertEqual(self.state["api_keys"], {})

    def test_storage_errors_do_not_leak(self):
        import psycopg
        for error, status in ((psycopg.OperationalError("private password"), 503),
                              (RuntimeError("private config"), 503),
                              (RuntimeError("account is pending deletion"), 403),
                              (LookupError("user not found"), 401)):
            with self.subTest(error=error), patch("storage.accounts.list_projects", side_effect=error):
                for response in (self.client.get("/api/projects/10/keys"), self.issue(),
                                 self.client.patch("/api/projects/10", json={"mode": "block",
                                                                             "csrf_token": self.csrf})):
                    self.assertEqual(response.status_code, status)
                    self.assertNotIn("private", response.text)
        with patch("storage.accounts.revoke_api_key", side_effect=psycopg.OperationalError("private password")):
            response = self.client.post("/api/keys/1/revoke", json={"csrf_token": self.csrf})
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private", response.text)


if __name__ == "__main__":
    unittest.main()
