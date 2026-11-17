"""C-19-9b (DM-120) : the live monitor additions. Offline only (fake upstream, fake DB rows, no LLM call).

Part 1 : per-step and cumulative tokens from the gateway's LLM-call rows ("측정 안 됨" when usage is missing).
Part 2 : the basis line (service/basis.py) for each signal combination, in the live API and the demo steps API.
Part 3 : the per-project utterance log — off by default, nothing stored for a project that is off, 1,000 characters,
no reasoning / request body / key, 7-day expiry, off deletes at once, someone else's project 404.
"""
from datetime import datetime, timedelta, timezone
import json
import re
from contextlib import contextmanager
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import httpx

from backend.routers import live as live_router
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.auth import service as auth_service
from backend.routers.keys import router as keys_router
from backend.routers.projects import router as projects_router
from service import basis, gateway
from storage import accounts, live
from test_c19_8k_keys_api import KeysFakeConnection, KeysFakeCursor, project as key_project
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
        self.live = FakeUtteranceLive()
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



ROOT = Path(__file__).resolve().parents[1]
SECRET = 'sk-utterancetestsecret0123456789abcdef'


class SchemaTests(unittest.TestCase):
    def test_off_by_default_cascading_and_bounded(self):
        schema = (ROOT / 'storage/schema.sql').read_text(encoding='utf-8')
        self.assertIn('ADD COLUMN IF NOT EXISTS utterance_log BOOLEAN NOT NULL DEFAULT FALSE', schema)
        table = schema[schema.index('CREATE TABLE IF NOT EXISTS utterances'):]
        table = table[:table.index(');')]
        self.assertIn('CHECK (char_length(excerpt) BETWEEN 1 AND 1000)', table)
        self.assertRegex(table, r'REFERENCES sessions\(id\)\s+ON DELETE CASCADE')
        self.assertIn('expires_at TIMESTAMPTZ NOT NULL', table)
        # Only the excerpt : no reasoning, request, key or body columns.
        self.assertNotRegex(table.lower(), r'reasoning|request|authorization|api_key|body')

    def test_new_projects_and_the_api_default_to_off(self):
        from backend.schemas import ProjectResponse
        shown = ProjectResponse.model_validate(key_project(10, 7, 'mine')).model_dump()
        self.assertIs(shown['utterance_log'], False)


class UtteranceDb:
    """An in-memory stand-in for the utterance statements of storage.live, with a clock the test moves."""

    def __init__(self, enabled):
        self.now = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
        self.enabled = enabled
        self.rows = {}
        self.statements = []

    @contextmanager
    def connection(self):
        yield mock.Mock(cursor=lambda: UtteranceCursor(self))


class UtteranceCursor:
    def __init__(self, db):
        self.db, self.rowcount, self.description, self.result = db, 0, None, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        q = ' '.join(sql.split())
        db = self.db
        db.statements.append((q, params))
        if q.startswith('SELECT id, status, source FROM sessions'):
            self.result = [('p10-s', 'active', 'live')] if params[0] == 'p10-s' else []
        elif q.startswith('SELECT p.utterance_log FROM sessions'):
            self.result = [(db.enabled,)]
        elif q.startswith('DELETE FROM utterances WHERE expires_at <= CURRENT_TIMESTAMP'):
            gone = [k for k, r in db.rows.items() if r['expires_at'] <= db.now]
            for k in gone:
                del db.rows[k]
            self.rowcount = len(gone)
        elif q.startswith('INSERT INTO utterances'):
            session_id, call_index, step, agent_id, excerpt, days = params
            db.rows.setdefault((session_id, call_index), {
                'call_index': call_index, 'step': step, 'agent_id': agent_id, 'excerpt': excerpt,
                'created_at': db.now, 'expires_at': db.now + timedelta(days=days)})
        elif q.startswith('SELECT call_index, step, agent_id, excerpt, created_at, expires_at FROM utterances'):
            names = ['call_index', 'step', 'agent_id', 'excerpt', 'created_at', 'expires_at']
            self.description = [type('C', (), {'name': n}) for n in names]
            self.result = [tuple(r[n] for n in names) for (sid, _), r in sorted(db.rows.items())
                           if sid == params[0] and r['expires_at'] > db.now]
        else:
            raise AssertionError('unexpected SQL: ' + q)

    def fetchone(self):
        return self.result[0] if self.result else None

    def fetchall(self):
        return self.result


class StorageUtteranceTests(unittest.TestCase):
    def use(self, enabled):
        db = UtteranceDb(enabled)
        self.enterContext(mock.patch.object(live, 'get_connection', db.connection))
        return db

    def test_a_project_that_is_off_stores_nothing(self):
        db = self.use(False)
        self.assertFalse(live.add_utterance('p10-s', 1, 1, None, '응답입니다'))
        self.assertEqual(db.rows, {})
        self.assertFalse(any(q.startswith('INSERT') for q, _ in db.statements))
        self.assertEqual(live.list_session_utterances('p10-s'), [])

    def test_a_project_that_is_on_keeps_the_first_1000_characters(self):
        db = self.use(True)
        self.assertTrue(live.add_utterance('p10-s', 1, 2, None, '  ' + '가' * 1500 + '  '))
        rows = live.list_session_utterances('p10-s')
        self.assertEqual((len(rows), len(rows[0]['excerpt']), rows[0]['step']), (1, 1000, 2))
        self.assertEqual(rows[0]['expires_at'] - rows[0]['created_at'], timedelta(days=7))
        self.assertTrue(live.add_utterance('p10-s', 1, 2, None, '다른 내용'))   # exact retry : ignored
        self.assertEqual(len(db.rows), 1)

    def test_text_with_a_key_pattern_or_no_text_is_not_stored(self):
        db = self.use(True)
        self.assertFalse(live.add_utterance('p10-s', 1, 1, None, '키는 ' + SECRET))
        self.assertFalse(live.add_utterance('p10-s', 2, 1, None, '   '))
        self.assertEqual(db.rows, {})
        with self.assertRaises(ValueError):
            live.add_utterance('p10-s', 3, 1, None, {'content': 'x'})
        with self.assertRaises(LookupError):
            live.add_utterance('p99-other', 1, 1, None, 'x')

    def test_rows_older_than_7_days_are_deleted_on_read_and_write(self):
        db = self.use(True)
        live.add_utterance('p10-s', 1, 1, None, '첫 응답')
        db.now += timedelta(days=6, hours=23)
        self.assertEqual(len(live.list_session_utterances('p10-s')), 1)
        db.now += timedelta(hours=1)
        self.assertEqual(live.list_session_utterances('p10-s'), [])
        self.assertEqual(db.rows, {})
        live.add_utterance('p10-s', 2, 2, None, '새 응답')
        db.now += timedelta(days=8)
        self.assertEqual(live.purge_expired_utterances(), 1)


class UtteranceSettingCursor(KeysFakeCursor):
    """The C-19-8K fake DB plus the utterance setting and the utterances table."""

    def execute(self, sql, params=()):
        q = ' '.join(sql.split())
        if 'FROM projects WHERE (owner_user_id = %s AND deleted_at IS NULL) OR (owner_user_id IS NULL AND read_only = TRUE)' in q:
            columns = ['id', 'name', 'mode', 'rule_id', 'provider', 'upstream_url', 'owner_user_id', 'read_only',
                       'created_at', 'utterance_log']
            rows = [tuple(p.get(c, False if c == 'utterance_log' else 'kmu') for c in columns)
                    for p in sorted(self.state['projects'].values(), key=lambda p: (not p['read_only'], p['id']))
                    if (p['owner_user_id'] == params[0] and p.get('deleted_at') is None)
                    or (p['owner_user_id'] is None and p['read_only'])]
            self._set_rows(columns, rows)
            return
        if q.startswith('UPDATE projects SET utterance_log = %s WHERE id = %s'):
            self.state['projects'][params[1]]['utterance_log'] = params[0]
            return
        if q.startswith('DELETE FROM utterances AS u USING sessions AS s, scenarios AS sc'):
            mine = [k for k, u in self.state['utterances'].items() if u['project_id'] == params[0]]
            for k in mine:
                del self.state['utterances'][k]
            self.rowcount = len(mine)
            return
        super().execute(sql, params)


class UtteranceSettingConnection(KeysFakeConnection):
    def cursor(self):
        return UtteranceSettingCursor(self.state)


class UtteranceSettingApiTests(unittest.TestCase):
    def setUp(self):
        self.state = {
            'next_key_id': 1,
            'users': {7: {'id': 7, 'account_status': 'active'}, 8: {'id': 8, 'account_status': 'active'}},
            'projects': {1: key_project(1, None, '데모', read_only=True), 10: key_project(10, 7, 'mine'),
                         11: key_project(11, 7, 'other of mine'), 20: key_project(20, 8, 'theirs')},
            'api_keys': {}, 'auth_events': [], 'executed_sql': [], 'due_user_ids': [],
            'utterances': {(10, 1): {'project_id': 10, 'excerpt': 'a'}, (10, 2): {'project_id': 10, 'excerpt': 'b'},
                           (11, 1): {'project_id': 11, 'excerpt': 'c'}},
        }
        db = mock.patch('storage.accounts.get_connection', side_effect=lambda: UtteranceSettingConnection(self.state))
        db.start()
        self.addCleanup(db.stop)
        state = mock.patch('backend.auth.service.fetch_account_state', return_value={
            'account_status': 'active', 'can_restore': None, 'deletion_requested_at': None, 'deletion_due_at': None})
        state.start()
        self.addCleanup(state.stop)
        app = FastAPI()
        app.include_router(projects_router)
        app.include_router(keys_router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.addCleanup(auth_service._sessions.clear)
        self.csrf = self.sign_in(7)

    def sign_in(self, user_id):
        token = auth_service.create_session({'id': user_id, 'sub': f'google-{user_id}'})
        self.client.cookies.set('guardrail_session', token)
        return auth_service.get_user(token)['csrf_token']

    def change(self, project_id, enabled, csrf=None):
        return self.client.patch(f'/api/projects/{project_id}', json={'utterance_log': enabled,
                                                                      'csrf_token': csrf or self.csrf})

    def test_on_then_off_deletes_that_projects_utterances_at_once(self):
        response = self.change(10, True)
        self.assertEqual((response.status_code, response.json()['utterance_log']), (200, True))
        self.assertEqual(len(self.state['utterances']), 3)            # on deletes nothing
        response = self.change(10, False)
        self.assertEqual((response.status_code, response.json()['utterance_log']), (200, False))
        self.assertEqual(list(self.state['utterances']), [(11, 1)])   # only project 10's rows went
        events = [e for e in self.state['auth_events'] if 'project_utterance_log_changed' in repr(e)]
        details = [getattr(e[-1], 'obj', e[-1]) for e in events]
        self.assertEqual(details, [{'project_id': 10, 'utterance_log': True, 'deleted': 0},
                                   {'project_id': 10, 'utterance_log': False, 'deleted': 2}])

    def test_someone_elses_project_is_404_the_demo_project_403_and_csrf_checked(self):
        self.assertEqual(self.change(20, True).status_code, 404)
        self.assertEqual(self.change(1, True).status_code, 403)
        self.assertEqual(self.change(10, True, csrf='wrong').status_code, 403)
        self.assertEqual(self.client.patch('/api/projects/10', json={'utterance_log': 'maybe',
                                                                     'csrf_token': self.csrf}).status_code, 422)
        self.assertIs(self.state['projects'][20].get('utterance_log'), None)

    def test_storage_refuses_a_non_boolean_and_a_project_not_owned(self):
        with self.assertRaises(ValueError):
            accounts.set_project_utterance_log(10, 7, 1)
        with self.assertRaises(LookupError):
            accounts.set_project_utterance_log(20, 7, True)


class FakeUtteranceLive(FakeLive):
    def __init__(self, fail=False):
        super().__init__()
        self.fail = fail

    def add_utterance(self, *args):
        self._check('add_utterance', *args)
        if self.fail:
            raise RuntimeError('db down')
        return True


class GatewayUtteranceTests(unittest.IsolatedAsyncioTestCase):
    async def start(self, responses, fail=False):
        self.enterContext(mock.patch.dict(os.environ, {'GUARDRAIL_PROVIDER_KMU_URL': KMU_URL}))
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))

        def upstream(request):
            return httpx.Response(200, json=responses.pop(0))
        self.live = FakeUtteranceLive(fail)
        app = gateway.create_app(accounts=fake_accounts(), live=self.live, transport=httpx.MockTransport(upstream),
                                 workdir=directory / 'gw', max_body=64 * 1024)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://mag.test')
        self.addAsyncCleanup(self.client.aclose)
        self.headers = {'X-Guardrail-Key': 'gk_kmuobs00_x', 'X-Guardrail-Session': 'u1',
                        'Authorization': 'Bearer ' + SECRET}

    async def chat(self, text='사용자 요청 원문 REQUEST-TEXT'):
        return await self.client.post('/v1/chat/completions', headers=self.headers, json={
            'model': DEMO_MODEL, 'messages': [{'role': 'user', 'content': text}]})

    async def test_only_the_assistant_content_goes_to_storage_cut_to_1000(self):
        await self.start([{'choices': [{'message': {'content': '답' * 1200, 'reasoning_content': 'REASONING-TEXT'}}],
                           'usage': {'prompt_tokens': 1, 'completion_tokens': 1}},
                          {'choices': [{'message': {'content': json.dumps(
                              {'narrative': '계획을 정리했습니다', 'open_issue_ids': []}, ensure_ascii=False)}}]},
                          {'choices': [{'message': {'content': None, 'tool_calls': [{'id': 't'}]}}]}])
        for _ in range(3):
            self.assertEqual((await self.chat()).status_code, 200)
        stored = self.live.named('add_utterance')
        self.assertEqual([(a[0], a[1], a[2], a[3]) for a in stored], [('p11-u1', 1, 1, None), ('p11-u1', 2, 1, None)])
        self.assertEqual((len(stored[0][4]), stored[1][4]), (1000, '계획을 정리했습니다'))
        everything = repr(self.live.calls)
        for absent in ('REASONING-TEXT', 'REQUEST-TEXT', SECRET):
            self.assertNotIn(absent, everything)

    async def test_a_failed_utterance_write_never_fails_the_request(self):
        await self.start([{'choices': [{'message': {'content': '응답'}}]}], fail=True)
        with self.assertLogs('service.gateway', level='WARNING') as logs:
            self.assertEqual((await self.chat()).status_code, 200)
        self.assertEqual(logs.output, ['WARNING:service.gateway:Utterance not recorded (RuntimeError)'])

    def test_excerpt_reads_content_or_the_report_narrative_only(self):
        excerpt = gateway.utterance_excerpt
        self.assertEqual(excerpt({'choices': [{'message': {'content': '```json\n{"narrative": "n"}\n```'}}]}), 'n')
        self.assertEqual(excerpt({'choices': [{'message': {'content': '{"status": "x"}'}}]}), '{"status": "x"}')
        self.assertIsNone(excerpt({'choices': [{'message': {'content': '', 'reasoning_content': 'r'}}]}))
        self.assertIsNone(excerpt({'error': {'code': 'guardrail_stop'}}))
        self.assertIsNone(excerpt('text'))


class LiveApiUtteranceTests(LiveApiTokenTests):
    def setUp(self):
        super().setUp()
        projects = {7: [dict(p, utterance_log=(p['id'] == 10)) for p in live_api.PROJECTS[7]],
                    8: live_api.PROJECTS[8]}
        self.enterContext(mock.patch.object(live_router.accounts, 'list_projects', lambda uid: projects[uid]))
        self.stored = {'p10-run-a': [{'call_index': 1, 'step': 1, 'agent_id': None, 'excerpt': '첫 응답'},
                                     {'call_index': 2, 'step': 2, 'agent_id': None, 'excerpt': '둘째 응답'}]}
        self.reads = []

        def utterances(session_id):
            self.reads.append(session_id)
            return self.stored.get(session_id, [])
        self.enterContext(mock.patch.object(live_router.live, 'list_session_utterances', utterances))

    def test_a_project_that_is_on_shows_each_steps_excerpt_in_the_detail_only(self):
        self.sign_in(7)
        session = self.client.get('/api/live/sessions/p10-run-a').json()['session']
        self.assertTrue(session['utterance_log'])
        self.assertEqual([r['said'] for r in session['step_rows']],
                         [[{'call': 1, 'text': '첫 응답'}], [{'call': 2, 'text': '둘째 응답'}], [], []])
        self.reads.clear()
        listing = self.client.get('/api/live/sessions').text
        self.assertNotIn('첫 응답', listing)
        self.assertEqual(self.reads, [])

    def test_a_project_that_is_off_is_not_read(self):
        with mock.patch.object(live_router.accounts, 'list_projects',
                               lambda uid: [dict(p, utterance_log=False) for p in live_api.PROJECTS[7]]):
            self.sign_in(7)
            session = self.client.get('/api/live/sessions/p10-run-a').json()['session']
        self.assertEqual((session['utterance_log'], self.reads), (False, []))
        self.assertTrue(all(r['said'] == [] for r in session['step_rows']))

if __name__ == '__main__':
    unittest.main()
