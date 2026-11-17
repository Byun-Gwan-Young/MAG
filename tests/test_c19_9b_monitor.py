"""C-19-9b (DM-120) : the live monitor additions. Offline only (fake upstream, fake DB rows, no LLM call).

Part 1 : per-step and cumulative tokens from the gateway's LLM-call rows ("측정 안 됨" when usage is missing).
Part 2 : the basis line (service/basis.py) for each signal combination, in the live API and the demo steps API.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import httpx

from backend.routers import live as live_router
from service import basis, gateway
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



def shape(n, agent='developer', state='stagnant', **values):
    return basis.step_shape(n=n, agent=agent, state=state, **values)


class BasisLineTests(unittest.TestCase):
    """One case per signal combination. Every value comes from the step shape ; nothing is read from text."""

    def test_threshold_is_the_frozen_consecutive_steps(self):
        self.assertEqual(basis.threshold(), 3)
        self.assertIsNone(basis.threshold('/nonexistent.json'))

    def test_progress_names_what_is_new(self):
        previous = shape(1, state='baseline', tests=[14, 16], paths=[], issue_ids=['ownership', 'sort'], adopted=3)
        now = shape(4, state='progress', tests=[15, 16], paths=['a.py', 'b.py'], issue_ids=['sort'], adopted=4)
        self.assertEqual(basis.line(now, previous, 3),
                         '새 파일 내용 2 개 · 통과 테스트 14 → 15 · 쟁점 ownership 닫힘 · 채택 3 → 4')

    def test_progress_of_a_dialogue_names_the_status_and_new_issues(self):
        previous = shape(1, state='baseline', status='접수', issue_ids=['a'])
        now = shape(4, state='progress', status='분류', issue_ids=['a', 'b'])
        self.assertEqual(basis.line(now, previous, 3), "새 쟁점 b · 처리 상태 '접수' → '분류'")

    def test_progress_without_a_fact_difference_reads_the_judge_events(self):
        now = shape(5, state='progress', paths=[], tests=[7, 17],
                    events=[{'metric': 'artifact.files_changed', 'item': 'SPEC.md', 'kind': 'new'},
                            {'metric': 'conversation.open_issues_count', 'item': 'requirements', 'kind': 'closed'}])
        self.assertEqual(basis.line(now, shape(2, tests=[7, 17], paths=[]), 3),
                         '새 파일 내용 1 개 (이 역할의 직전 관측 기준) · 쟁점 requirements 닫힘')
        self.assertEqual(basis.line(shape(5, state='progress'), None, 3),
                         '진전 : 신호가 바뀌었지만 세부 변화는 기록되지 않았습니다')

    def test_stagnant_names_what_stayed_and_the_counter(self):
        previous = shape(1, tests=[14, 15], paths=[], issue_ids=['a'])
        now = shape(4, tests=[14, 15], paths=[], issue_ids=['a'], consecutive=2)
        self.assertEqual(basis.line(now, previous, 3),
                         '파일 변화 없음 · 통과 테스트 14/15 그대로 · 새 쟁점 없음 → 진전 없는 차례 2/3')

    def test_stagnant_with_file_changes_but_no_new_test_maximum(self):
        now = shape(7, tests=[16, 17], paths=['src/x.py'], consecutive=1)
        self.assertEqual(basis.line(now, None, 3), '파일 1 개 바뀌었지만 · 통과 테스트 16/17 그대로 → 진전 없는 차례 1/3')

    def test_stagnant_dialogue_reports_status_and_adopted(self):
        now = shape(9, status='분류', adopted=2, issue_ids=['x'], consecutive=3)
        self.assertEqual(basis.line(now, shape(6, issue_ids=['x']), 3),
                         "새 쟁점 없음 · 처리 상태 '분류' 그대로 · 채택 2 그대로 → 진전 없는 차례 3/3")

    def test_oscillating_names_what_went_back(self):
        now = shape(8, state='oscillating', tests=[12, 16], revert_to=3, consecutive=2,
                    events=[{'metric': 'conversation.open_issues_count', 'item': 'ownership', 'kind': 'reopen'}])
        self.assertEqual(basis.line(now, None, 3), '되돌림 : 파일 상태가 3단계 내용으로 돌아감 · ownership 예전 값으로 돌아감 · '
                                                    '통과 테스트 12/16 → 진전 없는 차례 2/3')
        self.assertEqual(basis.line(shape(8, state='oscillating'), None, 3), '되돌림 : 예전 상태로 돌아감')

    def test_baseline_and_unmeasured_say_why(self):
        self.assertEqual(basis.line(shape(1, state='baseline'), None, 3),
                         '기준점 : 이 역할의 첫 관측이라 비교할 이전 기록이 없습니다')
        self.assertTrue(basis.line(shape(4, state='baseline'), shape(1), 3).startswith('기준점 : 비교 기준을 새로'))
        self.assertEqual(basis.line(shape(4, state='unmeasured', reason='metric selection changed'), None, 3),
                         '측정 불가 : 이전 관측과 지표 구성이 달라 비교할 수 없습니다')
        self.assertEqual(basis.line(shape(4, state='unmeasured', unmeasured_metrics=[
            'artifact.test_pass_count', 'artifact.files_changed']), None, 3),
            '측정 불가 : 통과 테스트 · 파일 변화 값을 측정하지 못했습니다')
        self.assertEqual(basis.line(shape(4, state='unmeasured'), None, 3),
                         '측정 불가 : 판정에 필요한 관측값이 기록되지 않았습니다')
        self.assertEqual(basis.line(shape(4, state=None), None, 3), '판정 기록 없음')

    def test_stop_keeps_the_recorded_sentence_and_block_names_the_403(self):
        stop = '최근 3 step 동안 역할 모두 진전이 없습니다. 통과 테스트 7/16.'
        self.assertEqual(basis.line(shape(9, stop_text=stop, consecutive=3), None, 3), stop)
        self.assertTrue(basis.line(shape(10, state=None, blocked=True), None, 3).startswith('차단 : '))

    def test_annotate_compares_with_the_same_role(self):
        steps = [shape(1, 'planner', 'baseline', issue_ids=['a', 'b']), shape(2, 'developer', 'baseline', tests=[1, 4]),
                 shape(3, 'planner', 'progress', issue_ids=['b']), shape(4, 'developer', 'progress', tests=[2, 4])]
        self.assertEqual(basis.annotate(steps, 3)[2:], ['쟁점 a 닫힘', '통과 테스트 1 → 2'])

    def test_adapters_read_the_recorded_shapes(self):
        live_row = {'n': 7, 'agent': 'developer', 'state': 'stagnant', 'stop': None, 'blocked': False,
                    'tests': [7, 16], 'paths': [], 'report': {'status': 's', 'adopted': ['x', 'y'],
                                                             'issue_ids': ['a']}}
        one = basis.from_live(live_row, {'state': 'stagnant', 't3': {'changed_paths': []}},
                              {'decision': 'continue', 'consecutive_steps': 2})
        self.assertEqual((one['tests'], one['paths'], one['adopted'], one['consecutive']), ((7, 16), [], 2, 2))
        fact = {'step_id': 3, 'agent': 'planner', 'state': 'stagnant', 'tests': {'passed': 5, 'total': 9},
                'changed_paths': [], 'report': {'status_label': '분류', 'adopted_items': 1, 'open_issue_ids': []},
                'judgement': {'consecutive_steps': 1}}
        two = basis.from_replay(fact)
        self.assertEqual((two['tests'], two['status'], two['issue_ids'], two['consecutive']), ((5, 9), '분류', [], 1))


class LiveApiBasisTests(LiveApiTokenTests):
    def test_live_rows_carry_the_basis_line(self):
        self.sign_in(7)
        rows = self.client.get('/api/live/sessions/p10-run-a').json()['session']['step_rows']
        self.assertEqual(rows[0]['basis'], '기준점 : 이 역할의 첫 관측이라 비교할 이전 기록이 없습니다')
        self.assertTrue(rows[1]['basis'].startswith('파일 변화 없음 · 통과 테스트 7/16 그대로 · 새 쟁점 없음'))
        self.assertEqual(rows[2]['basis'], live_api.STOP_TEXT)
        self.assertTrue(rows[3]['basis'].startswith('차단 : '))


if __name__ == '__main__':
    unittest.main()
