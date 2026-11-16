"""C-19-8P : each project picks its LLM provider from a fixed list (no address typed by the customer).

The routes (backend/routers/projects.py, keys.py) run in a FastAPI test client with a signed-in session ;
storage.accounts runs unchanged against the fake database of tests/test_c19_8k_keys_api.py (extended here with the
project insert and the provider update). Checked : the 16 fixed providers + kmu, ids outside the list rejected (API
and storage), no provider = the server default (the old call), kmu only when the server has its address, changing the
provider, the demo project 403, someone else's project 404, no address or server setting in any response, the
trailing "/" dropped. Offline : no request reaches a provider.
"""
import os
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.auth import service
from backend.routers.keys import router as keys_router
from backend.routers.projects import providers_router, router as projects_router
from storage import accounts
from tests.test_c19_8k_keys_api import NOW, KeysFakeConnection, KeysFakeCursor, project

KMU_ENV = "GUARDRAIL_PROVIDER_KMU_URL"
KMU_URL = "https://gateway.example.test/v1"

# The PM's table (guideline appendix C-19-8P, 2026-11-17), stored without a trailing "/".
EXPECTED = {
    "openai": ("OpenAI (GPT)", "global", "https://api.openai.com/v1"),
    "anthropic": ("Anthropic (Claude)", "global", "https://api.anthropic.com/v1"),
    "gemini": ("Google (Gemini)", "global", "https://generativelanguage.googleapis.com/v1beta/openai"),
    "xai": ("xAI (Grok)", "global", "https://api.x.ai/v1"),
    "mistral": ("Mistral", "global", "https://api.mistral.ai/v1"),
    "deepseek": ("DeepSeek", "global", "https://api.deepseek.com"),
    "groq": ("Groq", "global", "https://api.groq.com/openai/v1"),
    "together": ("Together AI", "global", "https://api.together.xyz/v1"),
    "fireworks": ("Fireworks AI", "global", "https://api.fireworks.ai/inference/v1"),
    "openrouter": ("OpenRouter", "global", "https://openrouter.ai/api/v1"),
    "perplexity": ("Perplexity", "global", "https://api.perplexity.ai"),
    "cohere": ("Cohere", "global", "https://api.cohere.ai/compatibility/v1"),
    "moonshot": ("Moonshot (Kimi)", "global", "https://api.moonshot.ai/v1"),
    "zai": ("Z.ai (GLM)", "global", "https://api.z.ai/api/paas/v4"),
    "upstage": ("Upstage (Solar)", "korea", "https://api.upstage.ai/v1"),
    "clova": ("네이버 (HyperCLOVA X)", "korea", "https://clovastudio.stream.ntruss.com/v1/openai"),
    "kmu": ("학교 게이트웨이 (팀 데모)", "team", None),
}
OUTSIDE = ("custom", "azure", "bedrock", "qwen", "open ai", "https://api.openai.com/v1", "", "  ")
# Storage trims and lowercases an id (the existing provider convention) ; the API takes the exact id only.
API_ONLY_OUTSIDE = ("OpenAI ", "OPENAI")


class ProviderFakeCursor(KeysFakeCursor):
    """Adds the project insert and the provider update to the 8K fake."""

    COLUMNS = ["id", "name", "mode", "rule_id", "provider", "upstream_url", "owner_user_id", "read_only",
               "created_at"]

    def execute(self, sql, params=()):
        q = " ".join(sql.split())
        if q.startswith("INSERT INTO projects"):
            self.state["executed_sql"].append(q)
            name, mode, rule_id, provider, upstream_url, owner = params
            pid = max(self.state["projects"]) + 1
            row = {"id": pid, "name": name, "mode": mode, "rule_id": rule_id, "provider": provider,
                   "upstream_url": upstream_url, "owner_user_id": owner, "read_only": False, "created_at": NOW}
            self.state["projects"][pid] = row
            self._set_rows(self.COLUMNS, [tuple(row[c] for c in self.COLUMNS)])
            return
        if q.startswith("UPDATE projects SET provider = %s, upstream_url = %s"):
            self.state["executed_sql"].append(q)
            provider, upstream_url, pid = params
            row = self.state["projects"][pid]
            row.update(provider=provider, upstream_url=upstream_url)
            self._set_rows(self.COLUMNS, [tuple(row[c] for c in self.COLUMNS)])
            return
        super().execute(sql, params)


class ProviderFakeConnection(KeysFakeConnection):
    def cursor(self):
        return ProviderFakeCursor(self.state)


def _env(**extra):
    """A clean provider environment : kmu default (the PM decision), extra on top."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("GUARDRAIL_PROVIDER_") and k not in (
               accounts.DEFAULT_PROVIDER_ENV, accounts.DEFAULT_UPSTREAM_URL_ENV, accounts.CUSTOM_PROVIDER_ENABLED_ENV)}
    env.update(extra)
    return patch.dict(os.environ, env, clear=True)


class ProviderTableTests(unittest.TestCase):
    def test_table_is_the_pm_list(self):
        rows = {pid: (name, group, url) for pid, name, group, url in accounts._PROVIDER_TABLE}
        self.assertEqual(rows, EXPECTED)
        self.assertEqual([r[0] for r in accounts._PROVIDER_TABLE], list(EXPECTED))   # screen order
        self.assertEqual(accounts.SELECTABLE_PROVIDER_IDS, frozenset(EXPECTED))
        self.assertNotIn("custom", accounts.SELECTABLE_PROVIDER_IDS)
        for pid, url in accounts._BUILTIN_PROVIDER_URLS.items():
            self.assertTrue(url.startswith("https://"), pid)
            self.assertFalse(url.endswith("/"), pid)

    def test_each_fixed_provider_resolves_to_its_fixed_address(self):
        with _env(GUARDRAIL_PROVIDER_OPENAI_URL="https://elsewhere.example.test/v1"):
            for pid, (_, _, url) in EXPECTED.items():
                if url:
                    self.assertEqual(accounts._resolve_selectable_provider(pid), (pid, url))
            # A server variable cannot move a fixed provider somewhere else.
            self.assertEqual(accounts._resolve_selectable_provider("openai")[1], "https://api.openai.com/v1")

    def test_ids_outside_the_list_are_rejected(self):
        with _env(GUARDRAIL_PROVIDER_AZURE_URL="https://azure.example.test/v1",
                  **{accounts.CUSTOM_PROVIDER_ENABLED_ENV: "1"}):
            for bad in OUTSIDE + (None, 3):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    accounts._resolve_selectable_provider(bad)
                self.assertFalse(accounts.provider_available(bad) if isinstance(bad, str) else False)
            self.assertEqual(accounts._resolve_selectable_provider("OpenAI ")[0], "openai")
            # Unknown ids are rejected even as the server default, whatever the environment says.
            with patch.dict(os.environ, {accounts.DEFAULT_PROVIDER_ENV: "azure"}):
                with self.assertRaises(ValueError):
                    accounts._get_default_provider_config()

    def test_kmu_only_with_its_server_address_and_trailing_slash_dropped(self):
        with _env():
            self.assertFalse(accounts.provider_available("kmu"))
            with self.assertRaises(ValueError):
                accounts._resolve_selectable_provider("kmu")
        with _env(**{KMU_ENV: KMU_URL + "/"}):
            self.assertTrue(accounts.provider_available("kmu"))
            self.assertEqual(accounts._resolve_selectable_provider("kmu"), ("kmu", KMU_URL))

    def test_list_providers_has_no_address(self):
        with _env(**{KMU_ENV: KMU_URL}):
            listed = accounts.list_providers()
        self.assertEqual([p["id"] for p in listed], list(EXPECTED))
        for p in listed:
            self.assertEqual(set(p), {"id", "name", "group", "available"})
            self.assertTrue(p["available"])
        self.assertNotIn("http", repr(listed))
        with _env():
            self.assertEqual([p["id"] for p in accounts.list_providers() if not p["available"]], ["kmu"])


class ProviderApiTests(unittest.TestCase):
    def setUp(self):
        self.state = {
            "next_key_id": 1,
            "users": {7: {"id": 7, "account_status": "active"}, 8: {"id": 8, "account_status": "active"}},
            "projects": {1: project(1, None, "데모", read_only=True), 10: project(10, 7, "mine"),
                         20: project(20, 8, "theirs")},
            "api_keys": {}, "auth_events": [], "executed_sql": [], "due_user_ids": [],
        }
        db = patch("storage.accounts.get_connection", side_effect=lambda: ProviderFakeConnection(self.state))
        db.start()
        self.addCleanup(db.stop)
        state = patch("backend.auth.service.fetch_account_state", return_value={
            "account_status": "active", "can_restore": None, "deletion_requested_at": None, "deletion_due_at": None})
        state.start()
        self.addCleanup(state.stop)
        env = _env(**{accounts.DEFAULT_PROVIDER_ENV: "kmu", KMU_ENV: KMU_URL})
        env.start()
        self.addCleanup(env.stop)
        app = FastAPI()
        app.include_router(projects_router)
        app.include_router(providers_router)
        app.include_router(keys_router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.addCleanup(service._sessions.clear)
        self.csrf = self.sign_in(7)

    def sign_in(self, user_id):
        token = service.create_session({"id": user_id, "sub": f"google-{user_id}"})
        self.client.cookies.set("guardrail_session", token)
        return service.get_user(token)["csrf_token"]

    def create(self, **fields):
        return self.client.post("/api/projects", json={"name": "새 프로젝트", "csrf_token": self.csrf, **fields})

    def change(self, pid, provider, **fields):
        return self.client.patch(f"/api/projects/{pid}",
                                 json={"provider": provider, "csrf_token": self.csrf, **fields})

    def assert_no_address(self, response):
        for url in [u for _, _, u in EXPECTED.values() if u] + [KMU_URL, "upstream_url"]:
            self.assertNotIn(url, response.text)

    def test_get_providers_needs_sign_in_and_has_no_address(self):
        response = self.client.get("/api/providers")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([p["id"] for p in response.json()], list(EXPECTED))
        self.assertEqual(response.json()[0], {"id": "openai", "name": "OpenAI (GPT)", "group": "global",
                                              "available": True})
        self.assert_no_address(response)
        self.assertNotIn("GUARDRAIL", response.text)
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/api/providers").status_code, 401)

    def test_kmu_not_available_without_server_address(self):
        with patch.dict(os.environ, {KMU_ENV: ""}):
            listed = {p["id"]: p["available"] for p in self.client.get("/api/providers").json()}
            self.assertFalse(listed["kmu"])
            self.assertEqual(self.create(provider="kmu").status_code, 422)
            self.assertEqual(self.change(10, "kmu").status_code, 422)
        self.assertEqual(len(self.state["projects"]), 3)
        self.assertEqual(self.state["projects"][10]["provider"], "kmu")   # unchanged

    def test_create_with_a_listed_provider(self):
        response = self.create(provider="anthropic", mode="block")
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual((body["provider"], body["mode"]), ("anthropic", "block"))
        self.assert_no_address(response)
        stored = self.state["projects"][body["id"]]
        self.assertEqual((stored["provider"], stored["upstream_url"], stored["owner_user_id"]),
                         ("anthropic", "https://api.anthropic.com/v1", 7))
        self.assertIn(("project_created", 7), [e[:2] for e in self.state["auth_events"]])

    def test_create_without_provider_uses_the_server_default(self):
        with patch("backend.routers.projects.accounts.create_project", wraps=accounts.create_project) as spy:
            response = self.create()
        self.assertEqual(response.status_code, 201)
        spy.assert_called_once_with(7, "새 프로젝트", "observe")          # the exact old call
        stored = self.state["projects"][response.json()["id"]]
        self.assertEqual((stored["provider"], stored["upstream_url"]), ("kmu", KMU_URL))

    def test_storage_create_project_provider_argument(self):
        with patch.dict(os.environ, {KMU_ENV: KMU_URL + "/"}):
            made = accounts.create_project(7, "a", "observe", "kmu")
        self.assertEqual(made["upstream_url"], KMU_URL)                    # trailing "/" dropped
        self.assertEqual(accounts.create_project(7, "b", "observe", "deepseek")["upstream_url"],
                         "https://api.deepseek.com")
        self.assertEqual(accounts.create_project(7, "c", "observe")["provider"], "kmu")   # old call
        for bad in OUTSIDE:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                accounts.create_project(7, "d", "observe", bad)

    def test_outside_the_list_rejected_by_the_api(self):
        before = dict(self.state["projects"])
        for bad in OUTSIDE + API_ONLY_OUTSIDE + (None, 5, ["openai"], "x" * 40):
            with self.subTest(bad=bad):
                if bad is not None:
                    self.assertEqual(self.create(provider=bad).status_code, 422)
                self.assertEqual(self.change(10, bad).status_code, 422)
        self.assertEqual(self.create(provider="openai", upstream_url="https://evil.example.test").status_code, 422)
        self.assertEqual(self.change(10, "openai", upstream_url="https://evil.example.test").status_code, 422)
        self.assertEqual(self.state["projects"], before)

    def test_change_provider(self):
        response = self.change(10, "gemini")
        self.assertEqual(response.status_code, 200)
        self.assertEqual((response.json()["id"], response.json()["provider"]), (10, "gemini"))
        self.assert_no_address(response)
        stored = self.state["projects"][10]
        self.assertEqual((stored["provider"], stored["upstream_url"]),
                         ("gemini", "https://generativelanguage.googleapis.com/v1beta/openai"))
        self.assertIn(("project_provider_changed", 7, {"project_id": 10, "provider": "gemini"}),
                      [(e[0], e[1], getattr(e[2], "obj", e[2])) for e in self.state["auth_events"]])
        both = self.change(10, "clova", mode="block")
        self.assertEqual((both.json()["provider"], both.json()["mode"]), ("clova", "block"))
        self.assertEqual(self.change(10, "clova").status_code, 200)        # same provider again : harmless

    def test_demo_403_and_someone_elses_404(self):
        self.assertEqual(self.change(1, "openai").status_code, 403)
        self.assertEqual(self.change(20, "openai").status_code, 404)
        self.assertEqual(self.change(999, "openai").status_code, 404)
        self.assertEqual(self.state["projects"][1]["provider"], "kmu")
        self.assertEqual(self.state["projects"][20]["provider"], "kmu")
        with self.assertRaises(LookupError):
            accounts.set_project_provider(20, 7, "openai")
        with self.assertRaises(LookupError):
            accounts.set_project_provider(1, 7, "openai")

    def test_csrf_and_empty_patch(self):
        self.assertEqual(self.client.patch("/api/projects/10", json={"provider": "openai", "csrf_token": "wrong"})
                         .status_code, 403)
        self.assertEqual(self.client.patch("/api/projects/10", json={"csrf_token": self.csrf}).status_code, 422)
        self.assertEqual(self.create(provider="openai", csrf_token="wrong").status_code, 403)
        self.assertEqual(self.state["projects"][10]["provider"], "kmu")

    def test_custom_stays_closed(self):
        with patch.dict(os.environ, {accounts.CUSTOM_PROVIDER_ENABLED_ENV: "1"}):
            self.assertEqual(self.create(provider="custom").status_code, 422)
            self.assertEqual(self.change(10, "custom").status_code, 422)
        self.assertNotIn("custom", [p["id"] for p in self.client.get("/api/providers").json()])


class ProjectListTests(unittest.TestCase):
    def test_list_query_selects_provider_but_not_the_address(self):
        from backend.repositories import projects as repo
        import inspect
        source = inspect.getsource(repo.fetch_projects)
        self.assertIn("provider", source)
        self.assertNotIn("upstream_url", source)

    def test_project_response_drops_the_address(self):
        from backend.schemas import ProjectResponse
        shown = ProjectResponse.model_validate(project(10, 7, "mine")).model_dump()
        self.assertEqual(shown["provider"], "kmu")
        self.assertNotIn("upstream_url", shown)


if __name__ == "__main__":
    unittest.main()
