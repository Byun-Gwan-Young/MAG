"""C-19-9c part 0b (DM-121) : DELETE /api/projects/{id}.

The route (backend/routers/keys.py), storage.accounts.delete_project, the service gateway's key check and the live
monitor's project list all run unchanged on an in-memory fake database (tests/test_c19_8k_keys_api.py's, extended
here with the scenarios · sessions · child tables and the delete statements). Checked : the project's keys are revoked
(rows kept), its sessions · observations · judgements · LLM calls · utterances · scenarios are deleted and nothing of
another project is, one `project_deleted` audit event (name and counts, no content), the project leaves the project
list · key API · live monitor, a deleted session is 404, a `/v1` request with its key is 401 afterwards (a running
session included), someone else's or a missing project 404, the demo project 403, CSRF. Offline : fake DB and upstream.
"""
from datetime import datetime, timezone
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import httpx

from backend.routers import live as live_router
from service import gateway
from storage import accounts
from tests.test_c19_8k_keys_api import NOW, KeysApiTests, KeysFakeConnection, KeysFakeCursor
from tests.test_c19_9_gateway import DEMO_MODEL, SECRET, FakeLive, ok
from tests.test_c19_9_live_api import judgement, observation, session_row

UPSTREAM = "https://gateway.example.test/v1"
CHILD_TABLES = ("utterances", "llm_calls", "judgements", "observations")


class DeleteFakeCursor(KeysFakeCursor):
    """The C-19-8K fake DB plus scenarios, sessions, their child rows and the statements of delete_project."""

    def _project_sessions(self, project_id):
        scenarios = {sid for sid, sc in self.state["scenarios"].items() if sc["project_id"] == project_id}
        return {sid for sid, s in self.state["sessions"].items() if s["scenario_id"] in scenarios}

    def execute(self, sql, params=()):
        q = " ".join(sql.split())
        if q.startswith("SELECT id, name FROM projects WHERE id = %s AND owner_user_id = %s AND read_only = FALSE "
                        "AND deleted_at IS NULL FOR UPDATE"):
            p = self.state["projects"].get(params[0])
            ok_row = p and p["owner_user_id"] == params[1] and not p["read_only"] and p.get("deleted_at") is None
            self._set_rows(["id", "name"], [(p["id"], p["name"])] if ok_row else [])
            return
        if q.startswith("UPDATE api_keys SET revoked_at = CURRENT_TIMESTAMP WHERE project_id = %s "
                        "AND revoked_at IS NULL RETURNING id"):
            ids = [k["id"] for k in self.state["api_keys"].values()
                   if k["project_id"] == params[0] and k["revoked_at"] is None]
            for key_id in ids:
                self.state["api_keys"][key_id]["revoked_at"] = NOW
            self._set_rows(["id"], [(i,) for i in ids])
            return
        if q.startswith("SELECT s.id FROM sessions AS s JOIN scenarios AS sc ON sc.id = s.scenario_id "
                        "WHERE sc.project_id = %s ORDER BY s.id FOR UPDATE OF s"):
            self._set_rows(["id"], [(s,) for s in sorted(self._project_sessions(params[0]))])
            return
        for table in CHILD_TABLES:
            if q.startswith(f"DELETE FROM {table} AS t USING sessions AS s, scenarios AS sc "
                            "WHERE t.session_id = s.id AND s.scenario_id = sc.id AND sc.project_id = %s"):
                mine = self._project_sessions(params[0])
                before = len(self.state[table])
                self.state[table] = [r for r in self.state[table] if r["session_id"] not in mine]
                self.rowcount = before - len(self.state[table])
                self._set_rows([], [])
                return
        if q.startswith("DELETE FROM sessions AS s USING scenarios AS sc WHERE s.scenario_id = sc.id "
                        "AND sc.project_id = %s"):
            mine = self._project_sessions(params[0])
            for sid in mine:
                del self.state["sessions"][sid]
            self.rowcount = len(mine)
            self._set_rows([], [])
            return
        if q.startswith("DELETE FROM scenarios WHERE project_id = %s"):
            mine = [sid for sid, sc in self.state["scenarios"].items() if sc["project_id"] == params[0]]
            for sid in mine:
                del self.state["scenarios"][sid]
            self.rowcount = len(mine)
            self._set_rows([], [])
            return
        if q.startswith("UPDATE projects SET deleted_at = CURRENT_TIMESTAMP, utterance_log = FALSE WHERE id = %s"):
            self.state["projects"][params[0]].update(deleted_at=NOW, utterance_log=False)
            self._set_rows([], [])
            return
        super().execute(sql, params)


class DeleteFakeConnection(KeysFakeConnection):
    def cursor(self):
        return DeleteFakeCursor(self.state)


def add_session(state, project_id, session_id, scenario_id):
    state["scenarios"][scenario_id] = {"project_id": project_id}
    state["sessions"][session_id] = {"scenario_id": scenario_id, "row": session_row(session_id)}
    for step in (1, 2):
        state["observations"].append({"session_id": session_id, "step": step})
        state["judgements"].append({"session_id": session_id, "step": step})
        state["llm_calls"].append({"session_id": session_id, "step": step})
        state["utterances"].append({"session_id": session_id, "step": step, "excerpt": f"{session_id} 응답"})


class ProjectDeleteApiTests(KeysApiTests):
    """Runs the C-19-8K key API tests again on this fake, plus the project-delete checks below."""

    def setUp(self):
        super().setUp()
        self.state.update(scenarios={}, sessions={}, **{t: [] for t in CHILD_TABLES})
        self.state["projects"][11] = {**self.state["projects"][10], "id": 11, "name": "my other"}
        add_session(self.state, 10, "p10-run-a", 100)
        add_session(self.state, 10, "p10-run-b", 101)
        add_session(self.state, 11, "p11-run-a", 110)
        add_session(self.state, 20, "p20-theirs", 200)
        db = mock.patch("storage.accounts.get_connection", side_effect=lambda: DeleteFakeConnection(self.state))
        db.start()
        self.addCleanup(db.stop)

    def delete(self, project_id=10, csrf=None, body=None):
        return self.client.request("DELETE", f"/api/projects/{project_id}",
                                   json=body if body is not None else {"csrf_token": csrf or self.csrf})

    def snapshot(self):
        return repr({k: v for k, v in self.state.items() if k != "executed_sql"})

    def events(self):
        return [(e[0], e[1], getattr(e[2], "obj", e[2])) for e in self.state["auth_events"]]

    def test_delete_revokes_the_keys_and_deletes_only_that_projects_records(self):
        live_key = self.issue(name="runner").json()
        old_key = self.issue(name="old").json()
        self.client.post(f"/api/keys/{old_key['id']}/revoke", json={"csrf_token": self.csrf})
        old_revoked_at = self.state["api_keys"][old_key["id"]]["revoked_at"] = datetime(2026, 11, 1, tzinfo=timezone.utc)
        other_key = self.issue(11, name="other").json()

        response = self.delete()
        self.assertEqual((response.status_code, response.json()), (200, {"id": 10, "deleted": True,
                                                                       "revoked_api_keys": 1}))
        # ① keys : revoked, rows kept (prefix · digest · revoked time), the earlier revocation time unchanged.
        self.assertIsNotNone(self.state["api_keys"][live_key["id"]]["revoked_at"])
        self.assertEqual(self.state["api_keys"][old_key["id"]]["revoked_at"], old_revoked_at)
        self.assertEqual(self.state["api_keys"][live_key["id"]]["key_hash"],
                         accounts._api_key_digest(live_key["api_key"]))
        self.assertIsNone(accounts.verify_api_key(live_key["api_key"]))
        self.assertIsNone(self.state["api_keys"][other_key["id"]]["revoked_at"])
        self.assertEqual(accounts.verify_api_key(other_key["api_key"])["id"], 11)
        # ② records : the project's sessions and every child table, utterances included ; other projects untouched.
        self.assertEqual(sorted(self.state["sessions"]), ["p11-run-a", "p20-theirs"])
        self.assertEqual(sorted(self.state["scenarios"]), [110, 200])
        for table in CHILD_TABLES:
            self.assertEqual(sorted({r["session_id"] for r in self.state[table]}), ["p11-run-a", "p20-theirs"], table)
        self.assertNotIn("p10-run-a 응답", repr(self.state))
        # The project row stays as a tombstone (the key rows point at it), hidden everywhere.
        self.assertIsNotNone(self.state["projects"][10]["deleted_at"])
        # ③ one audit event : name and counts, no content. The earlier events stay.
        deleted = [e for e in self.events() if e[0] == "project_deleted"]
        self.assertEqual(deleted, [("project_deleted", 7, {
            "project_id": 10, "name": "mine", "revoked_api_keys": 1,
            "deleted": {"utterances": 4, "llm_calls": 4, "judgements": 4, "observations": 4, "sessions": 2,
                        "scenarios": 2}})])
        self.assertIn(("api_key_revoked", 7, {"project_id": 10, "key_id": old_key["id"]}), self.events())

    def test_a_deleted_project_leaves_the_list_and_every_project_route(self):
        self.assertEqual(self.delete().status_code, 200)
        # The list the key page and the live monitor read (GET /api/projects : tests/test_backend_sessions.py).
        self.assertEqual([p["id"] for p in accounts.list_projects(7)], [1, 11])
        self.assertEqual(self.client.get("/api/projects/10/keys").status_code, 404)
        self.assertEqual(self.issue().status_code, 404)
        self.assertEqual(self.client.patch("/api/projects/10", json={"mode": "block", "csrf_token": self.csrf})
                         .status_code, 404)
        self.assertEqual(self.delete().status_code, 404)              # deleting it again
        self.assertEqual(len([e for e in self.events() if e[0] == "project_deleted"]), 1)

    def test_someone_elses_or_a_missing_project_is_404_the_demo_project_403(self):
        before = self.snapshot()
        self.assertEqual(self.delete(20).status_code, 404)
        self.assertEqual(self.delete(999).status_code, 404)
        self.assertEqual(self.delete(1).status_code, 403)
        self.assertEqual(self.delete(0).status_code, 422)
        self.assertEqual(self.snapshot(), before)
        # Storage refuses them on its own as well (the route is not the only guard).
        for project_id in (20, 1, 999):
            with self.assertRaises(LookupError):
                accounts.delete_project(project_id, 7)
        other_csrf = self.sign_in(8)
        self.assertEqual(self.delete(10, csrf=other_csrf).status_code, 404)
        self.assertIn("p10-run-a", self.state["sessions"])

    def test_csrf_is_required(self):
        before = self.snapshot()
        self.assertEqual(self.delete(csrf="wrong").status_code, 403)
        self.assertEqual(self.delete(body={}).status_code, 422)
        self.assertEqual(self.delete(body={"csrf_token": self.csrf, "cascade": True}).status_code, 422)
        self.assertEqual(self.snapshot(), before)

    def test_signed_out_and_recovery_sessions(self):
        super().test_signed_out_and_recovery_sessions()
        self.assertEqual(self.delete().status_code, 403)              # pending deletion : recovery required
        self.client.cookies.clear()
        self.assertEqual(self.delete(csrf="x").status_code, 401)
        self.assertIsNone(self.state["projects"][10].get("deleted_at"))

    def test_storage_errors_do_not_leak(self):
        super().test_storage_errors_do_not_leak()
        import psycopg
        with mock.patch("storage.accounts.delete_project", side_effect=psycopg.OperationalError("private password")):
            response = self.delete()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private", response.text)


class DeletedProjectGatewayTests(unittest.IsolatedAsyncioTestCase):
    """The gateway's key check is the real verify_api_key on the same fake DB the delete writes."""

    async def asyncSetUp(self):
        self.api = ProjectDeleteApiTests("test_csrf_is_required")
        self.api.setUp()
        self.addCleanup(self.api.doCleanups)
        self.enterContext(mock.patch.dict(os.environ, {"GUARDRAIL_PROVIDER_KMU_URL": UPSTREAM}))
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.upstream = []

        def upstream(request):
            self.upstream.append(request)
            return ok(request)
        app = gateway.create_app(accounts=accounts, live=FakeLive(), transport=httpx.MockTransport(upstream),
                                 workdir=directory / "gw", max_body=64 * 1024)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://mag.test")
        self.addAsyncCleanup(self.client.aclose)

    async def chat(self, raw_key, session):
        return await self.client.post("/v1/chat/completions", headers={
            "X-Guardrail-Key": raw_key, "X-Guardrail-Session": session, "Authorization": "Bearer " + SECRET,
        }, json={"model": DEMO_MODEL, "messages": [{"role": "user", "content": "hi"}]})

    async def test_v1_with_the_projects_key_is_401_after_the_delete(self):
        raw = self.api.issue().json()["api_key"]
        other = self.api.issue(11).json()["api_key"]
        self.assertEqual((await self.chat(raw, "run-1")).status_code, 200)
        self.assertEqual(self.api.delete().status_code, 200)
        for session in ("run-1", "run-2"):    # the running session and a new one
            response = await self.chat(raw, session)
            self.assertEqual((response.status_code, response.json()["error"]["code"]), (401, "guardrail_key_invalid"))
        self.assertEqual(len(self.upstream), 1)
        self.assertEqual((await self.chat(other, "run-1")).status_code, 200)   # the user's other project still works


class DeletedProjectMonitorTests(unittest.TestCase):
    """The live monitor (backend/routers/live.py) reads the project list from storage.accounts.list_projects."""

    def setUp(self):
        self.api = ProjectDeleteApiTests("test_csrf_is_required")
        self.api.setUp()
        self.addCleanup(self.api.doCleanups)
        state = self.api.state

        def fetch_sessions(conn, project_id, limit, offset, owner):
            rows = [s["row"] for s in state["sessions"].values()
                    if state["scenarios"][s["scenario_id"]]["project_id"] == project_id]
            return {"items": rows[offset:offset + limit], "total": len(rows), "limit": limit, "offset": offset}

        def fetch_session_detail(conn, project_id, session_id, owner):
            return next((r for r in fetch_sessions(conn, project_id, 50, 0, owner)["items"] if r["id"] == session_id),
                        None)

        def progress(session_id, after):
            if session_id not in state["sessions"]:
                raise LookupError("live session not found")
            return {"observations": [observation(1)], "judgements": [judgement(1, state="baseline")]}

        def calls(session_id):
            return [] if session_id in state["sessions"] else (_ for _ in ()).throw(LookupError("not found"))

        for name, value in (("fetch_sessions", fetch_sessions), ("fetch_session_detail", fetch_session_detail),
                            ("db_connection", mock.MagicMock())):
            self.enterContext(mock.patch.object(live_router, name, value))
        self.enterContext(mock.patch.object(live_router.live, "get_session_progress", progress))
        self.enterContext(mock.patch.object(live_router.live, "get_session_llm_calls", calls))
        self.enterContext(mock.patch.object(live_router.live, "list_session_utterances", lambda sid: []))
        self.enterContext(mock.patch.object(live_router.live, "purge_expired_utterances", lambda: 0))
        self.api.client.app.include_router(live_router.router)

    def test_deleted_sessions_leave_the_monitor_and_are_404(self):
        client = self.api.client
        listed = [s["id"] for s in client.get("/api/live/sessions").json()["sessions"]]
        self.assertEqual(sorted(listed), ["p10-run-a", "p10-run-b", "p11-run-a"])
        self.assertEqual(client.get("/api/live/sessions/p10-run-a").status_code, 200)
        self.assertEqual(self.api.delete().status_code, 200)
        listed = [s["id"] for s in client.get("/api/live/sessions").json()["sessions"]]
        self.assertEqual(listed, ["p11-run-a"])
        for session_id in ("p10-run-a", "p10-run-b"):
            self.assertEqual(client.get(f"/api/live/sessions/{session_id}").status_code, 404)
        self.assertEqual(client.get("/api/live/sessions/p11-run-a").status_code, 200)


if __name__ == "__main__":
    unittest.main()
