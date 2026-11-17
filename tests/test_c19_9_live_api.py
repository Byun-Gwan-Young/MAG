"""C-19-9 part 3 : the live dashboard API (backend/routers/live.py). Offline : signed-in test client, storage calls
and the backend's session queries replaced by in-memory data. Checked : only the user's own non-demo projects, the
session viewer's shape (steps, STOP sentence, 403 cell), someone else's / unknown session 404, sign-in required."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.auth import service
from backend.routers import live as live_router

T0 = datetime(2026, 11, 17, 4, 0, tzinfo=timezone.utc)
STOP_TEXT = '개발 역할이 세 번 연속 같은 결과를 보고했습니다.'
PROJECTS = {7: [{'id': 1, 'name': '데모', 'read_only': True, 'provider': 'kmu'},
                {'id': 10, 'name': '내 에이전트', 'read_only': False, 'provider': 'kmu'}],
            8: [{'id': 20, 'name': '남의 것', 'read_only': False, 'provider': 'openai'}]}


def session_row(sid, mode='observe', status='active', reason=None, tokens=None, started=T0):
    return {'id': sid, 'scenario_id': 1, 'status': status, 'termination_reason': reason, 'phase': None,
            'guardrail_mode': mode, 'model': 'glm-test', 'steps_reached': None, 'total_tokens': tokens,
            'first_stop_step': None, 'started_at': started, 'ended_at': None}


SESSIONS = {10: [session_row('p10-run-a', 'block', 'ended', 'guardrail_stop', 120),
                 session_row('p10-run-b', started=T0 + timedelta(minutes=5))],
            20: [session_row('p20-theirs')]}


def judgement(step, decision='continue', state='stagnant', stop=False):
    criteria = {'decision': decision, 'criteria_version': 'deadlock-v0.3'}
    if stop:
        criteria['explanation'] = {'template_id': 'E1', 'text': STOP_TEXT}
    return {'step': step, 'agent_id': 'developer', 'observed_at': T0 + timedelta(seconds=step),
            'decision': decision, 'result_data': {'collection': None, 'progress': {'state': state}, 'criteria': criteria}}


def observation(step):
    return {'step': step, 'agent_id': 'developer', 'observed_at': T0 + timedelta(seconds=step),
            'observation_data': {'step_id': step, 'artifact': {'test_pass_count': 7, 'test_total_count': 16},
                                 'conversation': {'source_kind': 'structured', 'open_issue_ids': ['a']}}}


PROGRESS = {'p10-run-a': {'observations': [observation(n) for n in range(1, 4)],
                          'judgements': [judgement(1, state='baseline'), judgement(2),
                                         judgement(3, 'deadlock', stop=True)]},
            'p10-run-b': {'observations': [observation(1)], 'judgements': [judgement(1, state='baseline')]},
            'p20-theirs': {'observations': [], 'judgements': []}}


# C-19-9b : the gateway's LLM-call rows per session (storage.live.get_session_llm_calls) ; none by default.
CALLS = {}


def llm_calls(session_id):
    if session_id not in PROGRESS:
        raise LookupError('live session not found')
    return CALLS.get(session_id, [])


@contextmanager
def no_db():
    yield object()


def fetch_sessions(conn, project_id, limit, offset, owner):
    mine = {p['id'] for p in PROJECTS[owner]}
    items = SESSIONS.get(project_id, []) if project_id in mine else []
    return {'items': items[offset:offset + limit], 'total': len(items), 'limit': limit, 'offset': offset}


def fetch_session_detail(conn, project_id, session_id, owner):
    return next((r for r in fetch_sessions(conn, project_id, 50, 0, owner)['items'] if r['id'] == session_id), None)


def progress(session_id, after):
    if session_id not in PROGRESS:
        raise LookupError('live session not found')
    return PROGRESS[session_id]


class LiveApiTests(unittest.TestCase):
    def setUp(self):
        for target, value in (('accounts.list_projects', lambda uid: PROJECTS[uid]), ('db_connection', no_db),
                              ('fetch_sessions', fetch_sessions), ('fetch_session_detail', fetch_session_detail),
                              ('live.get_session_progress', progress)):
            patcher = patch.object(*((live_router.accounts, 'list_projects') if target == 'accounts.list_projects'
                                     else (live_router.live, 'get_session_progress')
                                     if target == 'live.get_session_progress' else (live_router, target)), value)
            patcher.start()
            self.addCleanup(patcher.stop)
        calls = patch.object(live_router.live, 'get_session_llm_calls', llm_calls)
        calls.start()
        self.addCleanup(calls.stop)
        state = patch('backend.auth.service.fetch_account_state', return_value={
            'account_status': 'active', 'can_restore': None, 'deletion_requested_at': None, 'deletion_due_at': None})
        state.start()
        self.addCleanup(state.stop)
        app = FastAPI()
        app.include_router(live_router.router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.addCleanup(service._sessions.clear)

    def sign_in(self, user_id):
        self.client.cookies.set('guardrail_session', service.create_session({'id': user_id, 'sub': f'g-{user_id}'}))

    def test_sign_in_required(self):
        self.assertEqual(self.client.get('/api/live/sessions').status_code, 401)
        self.assertEqual(self.client.get('/api/live/sessions/p10-run-a').status_code, 401)

    def test_list_is_the_users_own_projects_newest_first(self):
        self.sign_in(7)
        response = self.client.get('/api/live/sessions')
        self.assertEqual((response.status_code, response.headers['cache-control']), (200, 'no-store'))
        rows = response.json()['sessions']
        self.assertEqual([r['id'] for r in rows], ['p10-run-b', 'p10-run-a'])
        a = rows[1]
        self.assertEqual((a['mode'], a['class'], a['first_stop'], a['blocked_step'], a['stop_text'], a['steps'],
                          a['tokens'], a['live']['status'], a['task_name'], a['session_key'], a['variant']),
                         ('block', 'stalled', 3, 4, STOP_TEXT, 3, 120, 'ended', '내 에이전트', 'run-a', 'run-a'))
        self.assertEqual((rows[0]['live']['status'], rows[0]['first_stop'], rows[0]['tokens']), ('running', None, None))

    def test_detail_has_the_viewer_steps_stop_sentence_and_the_403_cell(self):
        self.sign_in(7)
        session = self.client.get('/api/live/sessions/p10-run-a').json()['session']
        rows = session['step_rows']
        self.assertEqual([(r['n'], r['label'], r['blocked']) for r in rows],
                         [(1, '기준', False), (2, '정체', False), (3, '정체', False), (4, '차단 (403)', True)])
        self.assertEqual(rows[2]['stop'], {'t': 'E1', 'text': STOP_TEXT})
        self.assertEqual((rows[0]['tests'], rows[0]['report']['issues']), ([7, 16], 1))
        self.assertIsNone(rows[0]['tok'])

    def test_someone_elses_unknown_or_demo_session_is_404(self):
        self.sign_in(7)
        for sid in ('p20-theirs', 'p10-nothing', 'DEMO-CT-stall-v1-abc'):
            self.assertEqual(self.client.get(f'/api/live/sessions/{sid}').status_code, 404, sid)
        self.sign_in(8)
        self.assertEqual(self.client.get('/api/live/sessions/p10-run-a').status_code, 404)
        self.assertEqual([r['id'] for r in self.client.get('/api/live/sessions').json()['sessions']], ['p20-theirs'])

    def test_a_key_pattern_in_a_row_withholds_the_response(self):
        self.sign_in(7)
        explanation = PROGRESS['p10-run-a']['judgements'][2]['result_data']['criteria']['explanation']
        explanation['text'] = 'gk_abcdefgh_' + 'x' * 30
        try:
            self.assertEqual(self.client.get('/api/live/sessions/p10-run-a').status_code, 500)
            self.assertEqual(self.client.get('/api/live/sessions').status_code, 500)
        finally:
            explanation['text'] = STOP_TEXT

    def test_the_backend_app_serves_the_live_routes(self):
        from backend.main import app
        paths = set(app.openapi()['paths'])
        self.assertTrue({'/api/live/sessions', '/api/live/sessions/{session_id}'} <= paths)


if __name__ == '__main__':
    unittest.main()
