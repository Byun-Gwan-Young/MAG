"""C-19-9c part 0 (DM-121) : a revoked key leaves the key list, its record stays, and it is refused at once.

The key routes (backend/routers/keys.py) and the service gateway (service/gateway.py) both run on the real
storage/accounts.py over the in-memory fake database of tests/test_c19_8k_keys_api.py. Checked : GET lists live keys
only, the revoked row (digest · revoked time) and its auth event stay, verify_api_key refuses it, and a gateway `/v1`
request with the key is 401 right after the revoke (nothing goes upstream). Offline : fake DB, fake upstream.
"""
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import httpx

from service import gateway
from storage import accounts
from tests.test_c19_8k_keys_api import KeysApiTests
from tests.test_c19_9_gateway import DEMO_MODEL, SECRET, FakeLive, ok

UPSTREAM = "https://gateway.example.test/v1"   # the fake projects' kmu address (tests/test_c19_8k_keys_api.project)


class RevokedKeyListTests(KeysApiTests):
    """Runs the C-19-8K key API tests again (with the live-only list) plus the C-19-9c checks below."""

    def test_list_shows_live_keys_only_and_the_revocation_record_stays(self):
        first = self.issue(name="old runner").json()
        second = self.issue(name="new runner").json()
        response = self.client.post(f"/api/keys/{first['id']}/revoke", json={"csrf_token": self.csrf})
        self.assertEqual(response.status_code, 200)
        listing = self.client.get("/api/projects/10/keys")
        self.assertEqual(listing.status_code, 200)
        self.assertEqual([(k["id"], k["revoked"], k["revoked_at"]) for k in listing.json()],
                         [(second["id"], False, None)])
        self.assertNotIn("old runner", listing.text)
        # The DB keeps the revoked key's record for the audit trail : prefix, digest, revoked time.
        stored = self.state["api_keys"][first["id"]]
        self.assertEqual((stored["key_prefix"], stored["key_hash"]),
                         (first["key_prefix"], accounts._api_key_digest(first["api_key"])))
        self.assertIsNotNone(stored["revoked_at"])
        self.assertIn(("api_key_revoked", 7, {"project_id": 10, "key_id": first["id"]}),
                      [(e[0], e[1], getattr(e[2], "obj", e[2])) for e in self.state["auth_events"]])
        self.assertIsNone(accounts.verify_api_key(first["api_key"]))
        self.assertEqual(accounts.verify_api_key(second["api_key"])["id"], 10)
        # Revoking the same key again is still 200 and changes nothing.
        before = stored["revoked_at"]
        self.assertEqual(self.client.post(f"/api/keys/{first['id']}/revoke", json={"csrf_token": self.csrf})
                         .status_code, 200)
        self.assertEqual(self.state["api_keys"][first["id"]]["revoked_at"], before)

    def test_a_project_whose_keys_are_all_revoked_lists_nothing(self):
        key = self.issue().json()
        self.client.post(f"/api/keys/{key['id']}/revoke", json={"csrf_token": self.csrf})
        self.assertEqual(self.client.get("/api/projects/10/keys").json(), [])
        self.assertIn(key["id"], self.state["api_keys"])


class RevokedKeyGatewayTests(unittest.IsolatedAsyncioTestCase):
    """The gateway's key check is the real verify_api_key on the same fake DB the key API writes."""

    async def asyncSetUp(self):
        self.keys = RevokedKeyListTests("test_a_project_whose_keys_are_all_revoked_lists_nothing")
        self.keys.setUp()
        self.addCleanup(self.keys.doCleanups)
        self.enterContext(mock.patch.dict(os.environ, {"GUARDRAIL_PROVIDER_KMU_URL": UPSTREAM}))
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.upstream = []

        def upstream(request):
            self.upstream.append(request)
            return ok(request)
        self.live = FakeLive()
        app = gateway.create_app(accounts=accounts, live=self.live, transport=httpx.MockTransport(upstream),
                                 workdir=directory / "gw", max_body=64 * 1024)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://mag.test")
        self.addAsyncCleanup(self.client.aclose)

    async def chat(self, raw_key, session="s1"):
        return await self.client.post("/v1/chat/completions", headers={
            "X-Guardrail-Key": raw_key, "X-Guardrail-Session": session, "Authorization": "Bearer " + SECRET,
        }, json={"model": DEMO_MODEL, "messages": [{"role": "user", "content": "hi"}]})

    async def test_v1_is_401_right_after_the_revoke(self):
        issued = self.keys.issue().json()
        raw = issued["api_key"]
        self.assertEqual((await self.chat(raw)).status_code, 200)
        self.assertEqual(len(self.upstream), 1)
        revoke = self.keys.client.post(f"/api/keys/{issued['id']}/revoke", json={"csrf_token": self.keys.csrf})
        self.assertEqual(revoke.status_code, 200)
        for session in ("s1", "s2"):   # the running session and a new one
            response = await self.chat(raw, session)
            self.assertEqual((response.status_code, response.json()["error"]["code"]),
                             (401, "guardrail_key_invalid"))
        self.assertEqual(len(self.upstream), 1)          # nothing more went upstream
        for text in (raw, SECRET):
            self.assertNotIn(text, repr(self.keys.state))


if __name__ == "__main__":
    unittest.main()
