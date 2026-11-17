"""C-19-9b (DM-120) : the live monitor additions. Offline only (fake upstream, fake DB rows, no LLM call).

Part 1 : per-step and cumulative tokens from the gateway's LLM-call rows ("측정 안 됨" when usage is missing).
"""
from contextlib import contextmanager
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import httpx

from backend.routers import live as live_router
from service import gateway
from storage import live
import test_c19_9_live_api as live_api
from test_c19_9_gateway import DEMO_MODEL, KMU_URL, FakeLive, fake_accounts
from test_criteria_v03 import artifact_obs


def call(index, step, prompt, completion):
    return {'call_index': index, 'step': step, 'prompt_tokens': prompt, 'completion_tokens': completion}


class TokenColumnTests(unittest.TestCase):
    def test_step_tokens_sum_the_steps_calls_and_a_missing_usage_is_none(self):
        per = live_router._step_tokens([call(1, 1, 10, 5), call(2, 1, 20, 5), call(3, 2, None, 4), call(4, 2, 7, 1),
                                        call(5, None, 9, 9), call(6, 3, 0, 0)])
        self.assertEqual(per, {1: 40, 2: None, 3: 0})

    def test_running_total_stops_at_the_first_unmeasured_step(self):
        rows = [{'n': n} for n in (1, 2, 3, 4)]
        live_router._add_tokens(rows, [call(1, 1, 10, 5), call(2, 2, 1, 1), call(3, 3, None, 1), call(4, 4, 2, 2)])
        self.assertEqual([(r['tok'], r['cum']) for r in rows], [(15, 15), (2, 17), (None, None), (4, None)])

    def test_a_step_without_a_recorded_call_is_unmeasured_not_zero(self):
        rows = live_router._add_tokens([{'n': 1}, {'n': 2}], [call(1, 1, 3, 2)])
        self.assertEqual([(r['tok'], r['cum']) for r in rows], [(5, 5), (None, None)])


class LiveApiTokenTests(live_api.LiveApiTests):
    """The live API's step rows and list carry the token columns (the C-19-9 live API tests run again here)."""

    def setUp(self):
        super().setUp()
        live_api.CALLS['p10-run-a'] = [call(1, 1, 100, 20), call(2, 2, 90, 10), call(3, 3, 80, 0),
                                       call(4, 4, 0, 0)]   # the refused (403) request : nothing went upstream
        live_api.CALLS['p10-run-b'] = [call(1, 1, 50, 5), call(2, 2, 40, 4)]
        self.addCleanup(live_api.CALLS.clear)

    def test_detail_rows_have_this_step_and_cumulative_tokens(self):
        self.sign_in(7)
        rows = self.client.get('/api/live/sessions/p10-run-a').json()['session']['step_rows']
        self.assertEqual([(r['n'], r['tok'], r['cum']) for r in rows],
                         [(1, 120, 120), (2, 100, 220), (3, 80, 300), (4, 0, 300)])

    def test_a_running_session_lists_its_running_total(self):
        self.sign_in(7)
        rows = {r['id']: r for r in self.client.get('/api/live/sessions').json()['sessions']}
        # Ended : the gateway's own total. Running : the total of the steps judged so far (step 2's call has no
        # observation yet, so it is not counted).
        self.assertEqual((rows['p10-run-a']['tokens'], rows['p10-run-b']['tokens']), (120, 55))

    def test_detail_has_the_viewer_steps_stop_sentence_and_the_403_cell(self):
        live_api.CALLS.clear()
        super().test_detail_has_the_viewer_steps_stop_sentence_and_the_403_cell()

    def test_list_is_the_users_own_projects_newest_first(self):
        live_api.CALLS.clear()
        super().test_list_is_the_users_own_projects_newest_first()


class StorageLlmCallsTests(unittest.TestCase):
    def test_reader_selects_token_metadata_only_for_a_live_session(self):
        executed = []

        class Cursor:
            description = None

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, sql, params=()):
                executed.append((' '.join(sql.split()), params))
                if 'FROM sessions' in sql:
                    self.row = ('p1-a', 'active', 'live')
                else:
                    self.description = [type('C', (), {'name': n}) for n in
                                        ('call_index', 'step', 'prompt_tokens', 'completion_tokens')]
                    self.rows = [(1, 1, 10, 2)]

            def fetchone(self):
                return self.row

            def fetchall(self):
                return self.rows

        @contextmanager
        def connection():
            yield mock.Mock(cursor=Cursor)

        with mock.patch.object(live, 'get_connection', connection):
            self.assertEqual(live.get_session_llm_calls('p1-a'),
                             [{'call_index': 1, 'step': 1, 'prompt_tokens': 10, 'completion_tokens': 2}])
        query = executed[-1][0]
        self.assertIn('SELECT call_index, step, prompt_tokens, completion_tokens FROM llm_calls', query)
        with self.assertRaises(ValueError):
            live.get_session_llm_calls('')


class GatewayTokenTests(unittest.IsolatedAsyncioTestCase):
    """The gateway records each call's usage (input · output) ; a response without usage is recorded as None."""

    async def asyncSetUp(self):
        self.enterContext(mock.patch.dict(os.environ, {'GUARDRAIL_PROVIDER_KMU_URL': KMU_URL}))
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.responses = [{'choices': [{'message': {'content': 'a'}}],
                           'usage': {'prompt_tokens': 30, 'completion_tokens': 7, 'total_tokens': 37}},
                          {'choices': [{'message': {'content': 'b'}}]}]

        def upstream(request):
            return httpx.Response(200, json=self.responses.pop(0))
        self.live = FakeLive()
        app = gateway.create_app(accounts=fake_accounts(), live=self.live, transport=httpx.MockTransport(upstream),
                                 workdir=directory / 'gw', max_body=64 * 1024)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://mag.test')
        self.addAsyncCleanup(self.client.aclose)

    async def test_usage_is_recorded_per_call_and_missing_usage_is_none(self):
        headers = {'X-Guardrail-Key': 'gk_kmuobs00_x', 'X-Guardrail-Session': 't1', 'Authorization': 'Bearer x'}
        for step in (1, 2):
            response = await self.client.post('/v1/chat/completions', headers=headers, json={
                'model': DEMO_MODEL, 'messages': [{'role': 'user', 'content': 'hi'}]})
            self.assertEqual(response.status_code, 200)
            response = await self.client.post('/sessions/observations', headers=headers,
                                              json=artifact_obs(step, {'a.py': str(step)}))
            self.assertEqual(response.status_code, 200, response.text)
        calls = [(a[1], a[2], a[4], a[5]) for a in self.live.named('add_llm_call')]
        self.assertEqual(calls, [(1, 1, 30, 7), (2, 2, None, None)])


if __name__ == '__main__':
    unittest.main()
