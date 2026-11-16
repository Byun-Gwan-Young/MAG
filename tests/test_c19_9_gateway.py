"""C-19-9 part 1 (DM-92, DM-119) : the service gateway (service/gateway.py). Offline only : a fake upstream
(httpx.MockTransport), fake key checks, and a fake DB whose every write first passes the real storage.live argument
checks (the connection is stubbed out right after them)."""
import contextlib
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import httpx

import proxy
from progress_schema import SessionRegistration
from service import gateway
from storage import accounts, live
from test_criteria_v03 import artifact_obs

ROOT = Path(__file__).resolve().parents[1]
DEMO_MODEL = gateway.demo_model()
KMU_URL = 'https://kmu-gateway.example/v1/gateway'
SECRET = 'sk-gatewaytestsecret0123456789abcdef'
KEYS = {
    'gk_kmuobs00_x': {'id': 11, 'mode': 'observe', 'upstream_url': KMU_URL},
    'gk_kmublk00_x': {'id': 12, 'mode': 'block', 'upstream_url': KMU_URL},
    'gk_openai00_x': {'id': 13, 'mode': 'observe', 'upstream_url': 'https://api.openai.com/v1'},
    'gk_custom00_x': {'id': 14, 'mode': 'observe', 'upstream_url': 'https://evil.example/v1'},
    'gk_demo0000_x': {'id': 1, 'mode': 'observe', 'upstream_url': KMU_URL, 'read_only': True},
}


class Validated(Exception):
    pass


class FakeLive:
    """storage.live with an in-memory DB. Each call first runs the real function up to its DB connection."""

    def __init__(self):
        self.calls, self.sessions = [], {}

    def _check(self, name, *args):
        with mock.patch.object(live, 'get_connection', side_effect=Validated):
            try:
                getattr(live, name)(*args)
            except Validated:
                pass
        self.calls.append((name, args))

    def ensure_live_scenario(self, *args):
        self._check('ensure_live_scenario', *args)
        return 1

    def start_live_session(self, project_id, session_id, *args):
        self._check('start_live_session', project_id, session_id, *args)
        if session_id in self.sessions:
            raise ValueError('session_id already exists with different live-session data')
        self.sessions[session_id] = {'project_id': project_id, 'mode': args[2], 'model': args[3]}

    def add_llm_call(self, *args):
        self._check('add_llm_call', *args)

    def add_observation(self, *args):
        self._check('add_observation', *args)

    def add_judgement(self, *args):
        self._check('add_judgement', *args)

    def end_live_session(self, *args):
        self._check('end_live_session', *args)

    def get_session_progress(self, session_id, after_step):
        if session_id not in self.sessions:
            raise LookupError('live session not found')
        return {}

    def named(self, name):
        return [args for n, args in self.calls if n == name]


def fake_accounts():
    def verify(raw):
        project = KEYS.get(raw)
        return None if project is None else {'read_only': False, 'name': 'p', **project}
    return mock.Mock(verify_api_key=verify, allowed_upstreams=accounts.allowed_upstreams)


def ok(request):
    return httpx.Response(200, json={'choices': [{'message': {'content': 'ok'}}],
                                     'usage': {'prompt_tokens': 3, 'completion_tokens': 2, 'total_tokens': 5}})


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(mock.patch.dict(os.environ, {'GUARDRAIL_PROVIDER_KMU_URL': KMU_URL}))
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.upstream = []

        def upstream(request):
            self.upstream.append(request)
            return ok(request)
        self.live = FakeLive()
        self.app = gateway.create_app(accounts=fake_accounts(), live=self.live, transport=httpx.MockTransport(upstream),
                                      workdir=self.directory / 'gw', max_body=64 * 1024)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='https://mag.test')
        self.addAsyncCleanup(self.client.aclose)

    def headers(self, key='gk_kmuobs00_x', session='s1', **extra):
        return {'X-Guardrail-Key': key, 'X-Guardrail-Session': session, 'Authorization': 'Bearer ' + SECRET, **extra}

    async def chat(self, headers, model=DEMO_MODEL, **body):
        return await self.client.post('/v1/chat/completions', headers=headers,
                                      json={'model': model, 'messages': [{'role': 'user', 'content': 'hi'}], **body})

    async def run_steps(self, headers, steps=6):
        for step in range(1, steps + 1):
            self.assertEqual((await self.chat(headers)).status_code, 200)
            response = await self.client.post('/sessions/observations', headers=headers,
                                              json=artifact_obs(step, {'code.py': 'same'}))
            self.assertEqual(response.status_code, 200, response.text)
        return response.json()['observation']

    # --- keys ---

    async def test_missing_unknown_or_revoked_key_is_401_and_nothing_goes_upstream(self):
        for key in (None, 'gk_revoked0_x', 'not-a-key'):
            headers = self.headers()
            if key is None:
                headers.pop('X-Guardrail-Key')
            else:
                headers['X-Guardrail-Key'] = key
            for path, body in (('/v1/chat/completions', {'model': DEMO_MODEL, 'messages': []}),
                               ('/sessions', None), ('/sessions/observations', artifact_obs(1, {'a.py': 'x'})),
                               ('/sessions/end', {})):
                response = await self.client.post(path, headers=headers, json=body)
                self.assertEqual(response.status_code, 401, (key, path))
                self.assertIn(response.json()['error']['code'], ('guardrail_key_required', 'guardrail_key_invalid'))
        self.assertEqual((self.upstream, self.live.calls), ([], []))

    async def test_demo_project_key_is_403(self):
        response = await self.chat(self.headers('gk_demo0000_x'))
        self.assertEqual((response.status_code, response.json()['error']['code']), (403, 'project_read_only'))
        self.assertEqual(self.upstream, [])

    async def test_someone_elses_session_is_404_and_the_same_key_name_stays_separate(self):
        await self.chat(self.headers())
        other = self.headers('gk_openai00_x')
        for path in ('/sessions/observations', '/sessions/end'):
            response = await self.client.post(path, headers=other, json=artifact_obs(1, {'a.py': 'x'}))
            self.assertEqual((response.status_code, response.json()), (404, {'detail': 'Unknown session'}))
        self.assertEqual((await self.client.get('/log', headers=other)).status_code, 404)
        # The other project's own "s1" is its own session (DB ids differ by project).
        self.assertEqual((await self.chat(other, model='gpt-test')).status_code, 200)
        self.assertEqual(sorted(self.live.sessions), ['p11-s1', 'p13-s1'])

    # --- upstream allow-list ---

    async def test_upstream_is_only_the_projects_provider_address(self):
        response = await self.chat(self.headers('gk_openai00_x', **{'X-Guardrail-Upstream': 'https://evil.example'}),
                                   model='gpt-test', base_url='https://evil.example/v1')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(str(self.upstream[-1].url), 'https://api.openai.com/v1/chat/completions')
        await self.chat(self.headers(session='s2'))
        # kmu : the server's address with the gateway's documented trailing slash (as the demo configuration).
        self.assertEqual(str(self.upstream[-1].url), KMU_URL + '/chat/completions/')

    async def test_an_address_outside_the_allow_list_is_refused_before_anything_is_sent(self):
        response = await self.chat(self.headers('gk_custom00_x'))
        self.assertEqual((response.status_code, response.json()['error']['code']), (403, 'upstream_not_allowed'))
        with mock.patch.dict(os.environ, {'GUARDRAIL_PROVIDER_KMU_URL': ''}):
            response = await self.chat(self.headers())   # kmu without a server address : not in the list either
        self.assertEqual((response.status_code, response.json()['error']['code']), (403, 'upstream_not_allowed'))
        self.assertEqual((self.upstream, self.live.calls), ([], []))

    def test_allow_list_is_the_provider_table(self):
        with mock.patch.dict(os.environ, {'GUARDRAIL_PROVIDER_KMU_URL': ''}):
            allowed = accounts.allowed_upstreams()
        self.assertNotIn('kmu', allowed.values())
        self.assertEqual(allowed['https://api.openai.com/v1'], 'openai')
        self.assertEqual(len(allowed), 16)
        self.assertEqual(accounts.allowed_upstreams()[KMU_URL], 'kmu')
        self.assertNotIn('custom', accounts.allowed_upstreams().values())

    # --- LLM key ---

    async def test_llm_key_is_forwarded_unchanged_and_never_logged_or_stored(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        root = logging.getLogger()
        root.addHandler(handler)
        previous = root.level
        root.setLevel(logging.DEBUG)
        try:
            await self.run_steps(self.headers('gk_kmublk00_x'))
            blocked = await self.chat(self.headers('gk_kmublk00_x'))
            await self.client.post('/sessions/end', headers=self.headers('gk_kmublk00_x'),
                                   json={'termination_reason': 'guardrail_stop'})
            log = await self.client.get('/log', headers=self.headers('gk_kmublk00_x'))
        finally:
            root.removeHandler(handler)
            root.setLevel(previous)
        self.assertEqual(blocked.status_code, 403)
        self.assertEqual({r.headers['authorization'] for r in self.upstream}, {'Bearer ' + SECRET})
        self.assertTrue(all('x-guardrail-key' not in r.headers for r in self.upstream))
        self.assertNotIn(SECRET, stream.getvalue())
        self.assertNotIn(SECRET, json.dumps(self.live.calls, default=str))
        self.assertNotIn(SECRET, blocked.text + log.text)
        self.assertNotIn('gk_kmublk00_x', json.dumps(self.live.calls, default=str) + stream.getvalue())

    # --- DB records ---

    async def test_observe_session_is_written_to_the_db_step_by_step(self):
        headers = self.headers(**{'X-Guardrail-Scenario': 'my-agents'})
        stop = await self.run_steps(headers)
        self.assertEqual(stop['criteria']['decision'], 'deadlock')
        ((pid, key, hash_, name, config),) = self.live.named('ensure_live_scenario')
        self.assertEqual((pid, key, name, config['scenario_key']), (11, 'my-agents', 'my-agents', 'my-agents'))
        ((_, db_id, scenario, scenario_hash, mode, model, execution),) = self.live.named('start_live_session')
        self.assertEqual((db_id, scenario, scenario_hash, mode, model), ('p11-s1', 'my-agents', hash_, 'observe',
                                                                         DEMO_MODEL))
        self.assertEqual((execution['provider'], execution['criteria_version'], execution['phase']),
                         ('kmu', 'deadlock-v0.3', 'demo'))
        calls = self.live.named('add_llm_call')
        self.assertEqual([(c[1], c[2], c[4], c[5], c[8]) for c in calls], [(n, n, 3, 2, 200) for n in range(1, 7)])
        judgements = self.live.named('add_judgement')
        self.assertEqual([j[1] for j in judgements], list(range(1, 7)))
        self.assertEqual([j[3] for j in judgements][-1], 'deadlock')
        result = judgements[-1][9]
        self.assertEqual(result['criteria']['explanation']['text'], stop['criteria']['explanation']['text'])
        self.assertEqual(set(result), {'collection', 'progress', 'criteria'})
        observation = self.live.named('add_observation')[0][3]
        self.assertNotIn('progress', observation)
        self.assertEqual(observation['step_id'], 1)
        response = await self.client.post('/sessions/end', headers=headers, json={'termination_reason': 'max_steps'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.live.named('end_live_session'), [('p11-s1', 'max_steps', 6, 30, 6)])

    async def test_runner_style_session_start_uses_the_demo_pin_and_log_carries_meta_only(self):
        headers = self.headers(session='run-1')
        registration = {'registration_version': 'session-registration-v1',
                        'role_labels': {'planner': '기획', 'developer': '개발', 'reviewer': '검토'},
                        'progress_source': 'artifact',
                        'progress_metric': {'artifact': ['test_pass_count', 'files_changed']}}
        response = await self.client.post('/sessions', headers=headers, json={'registration': registration})
        self.assertEqual((response.status_code, response.json()), (200, {'session_id': 'run-1', 'registered': True}))
        log = (await self.client.get('/log', headers=headers)).json()
        self.assertEqual(set(log), {'session_id', 'status', 'meta'})
        self.assertEqual(log['meta']['pinned_model']['name'], DEMO_MODEL)
        self.assertEqual(log['meta']['pinned_model']['source'], 'run_conditions')
        registration = SessionRegistration.model_validate(registration).model_dump()
        self.assertEqual(log['meta']['registration'], registration)
        self.assertEqual(log['meta']['upstream']['api_key_env'], 'KMU_API_KEY')
        config = self.live.named('ensure_live_scenario')[0][4]
        self.assertEqual(config['registration'], registration)
        again = await self.client.post('/sessions', headers=headers)
        self.assertEqual(again.status_code, 409)
        conditions = await self.client.get('/conditions', headers=self.headers())
        self.assertEqual((conditions.json()['guardrail_mode'], conditions.json()['pinned_model']['name']),
                         ('observe', DEMO_MODEL))

    async def test_session_start_needs_a_model_outside_kmu(self):
        response = await self.client.post('/sessions', headers=self.headers('gk_openai00_x'))
        self.assertEqual((response.status_code, response.json()['error']['code']), (400, 'model_required'))
        response = await self.client.post('/sessions', headers=self.headers('gk_openai00_x',
                                                                             **{'X-Guardrail-Model': 'gpt-test'}))
        self.assertEqual(response.status_code, 200)

    async def test_a_session_keeps_its_first_model(self):
        await self.chat(self.headers('gk_openai00_x'), model='gpt-a')
        response = await self.chat(self.headers('gk_openai00_x'), model='gpt-b')
        self.assertEqual((response.status_code, response.json()['error']['code']),
                         (409, 'model_differs_from_pinned_model'))
        self.assertEqual(len(self.upstream), 1)

    async def test_a_session_recorded_before_a_restart_is_refused(self):
        self.live.sessions['p11-old'] = {}
        response = await self.chat(self.headers(session='old'))
        self.assertEqual((response.status_code, response.json()['error']['code']),
                         (409, 'session_recorded_before_restart'))
        self.assertEqual(self.upstream, [])

    async def test_without_session_header_the_first_message_names_the_session(self):
        headers = self.headers()
        headers.pop('X-Guardrail-Session')
        response = await self.chat(headers)
        key = 'hash-' + hashlib.sha256(json.dumps({'role': 'user', 'content': 'hi'}, sort_keys=True).encode()).hexdigest()
        self.assertEqual(response.headers['X-Guardrail-Session'], key)
        response = await self.client.post('/v1/chat/completions', headers=headers, json={'model': DEMO_MODEL,
                                                                                         'messages': []})
        self.assertEqual((response.status_code, response.json()['error']['code']), (400, 'session_key_required'))

    async def test_request_size_limit_is_413(self):
        response = await self.chat(self.headers(), padding='x' * (65 * 1024))
        self.assertEqual((response.status_code, response.json()['error']['code']), (413, 'request_too_large'))
        self.assertEqual(self.upstream, [])

    async def test_upstream_504_is_passed_through_and_recorded(self):
        app = gateway.create_app(accounts=fake_accounts(), live=self.live,
                                 transport=httpx.MockTransport(lambda r: httpx.Response(504, json={'error': 'timeout'})),
                                 workdir=self.directory / 'gw2')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://mag.test') as client:
            response = await client.post('/v1/chat/completions', headers=self.headers(),
                                         json={'model': DEMO_MODEL, 'messages': []})
        self.assertEqual((response.status_code, response.json()), (504, {'error': 'timeout'}))
        self.assertEqual(self.live.named('add_llm_call')[0][8], 504)

    def test_upstream_read_limit_is_the_proxys_600_seconds(self):
        self.assertEqual(proxy.UPSTREAM_TIMEOUT.read, 600)
        inner = self.app.state.gateway.inner_app((11, 'observe', 'kmu', KMU_URL, DEMO_MODEL))
        self.assertIs(inner.app.state.store.repository, inner.repository)


class BlockBodyTests(unittest.IsolatedAsyncioTestCase):
    """The gateway's block-mode refusal is the Mac block proxy's : same status, headers and JSON body."""

    async def test_403_after_stop_matches_the_mac_proxy(self):
        self.enterContext(mock.patch.dict(os.environ, {'GUARDRAIL_PROVIDER_KMU_URL': KMU_URL}))
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        sent = {'mac': 0, 'gateway': 0}

        def counting(name):
            def handler(request):
                sent[name] += 1
                return ok(request)
            return httpx.MockTransport(handler)
        mac = proxy.create_app(directory / 'logs', counting('mac'),
                               config_path=ROOT / 'guardrail-deadlock-v0.3-kmu-block.yaml')
        gw = gateway.create_app(accounts=fake_accounts(), live=FakeLive(), transport=counting('gateway'),
                                workdir=directory / 'gw')
        results = {}
        for name, app, extra in (('mac', mac, {'X-Guardrail-Phase': 'demo'}),
                                 ('gateway', gw, {'X-Guardrail-Key': 'gk_kmublk00_x'})):
            headers = {'X-Guardrail-Session': 'blocked', 'Authorization': 'Bearer ' + SECRET, **extra}
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://t') as client:
                for step in range(1, 7):
                    await client.post('/v1/chat/completions', headers=headers,
                                      json={'model': DEMO_MODEL, 'messages': []})
                    await client.post('/sessions/observations', headers=headers,
                                      json=artifact_obs(step, {'code.py': 'same'}))
                response = await client.post('/v1/chat/completions', headers=headers,
                                             json={'model': DEMO_MODEL, 'messages': [{'content': 'next'}]})
            results[name] = (response.status_code, response.json(),
                             {k: response.headers.get(k) for k in ('x-should-retry', 'x-guardrail-turn',
                                                                   'x-guardrail-session')})
        self.assertEqual(results['gateway'], results['mac'])
        self.assertEqual(results['mac'][0], 403)
        self.assertEqual(results['mac'][1]['error']['code'], 'guardrail_stop')
        self.assertEqual(sent, {'mac': 6, 'gateway': 6})


class StorageHelperTests(unittest.TestCase):
    def test_ensure_live_scenario_checks_then_uses_the_importer_upsert(self):
        executed = []

        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, params=()):
                executed.append((' '.join(sql.split())[:40], params))

            def fetchone(self):
                return (7, 'observe', False, 'kmu', 3) if len(executed) == 1 else (42,)

        class Conn:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def cursor(self):
                return Cursor()
        with mock.patch.object(live, 'get_connection', return_value=Conn()):
            self.assertEqual(live.ensure_live_scenario(7, 'live', 'h' * 64, 'live', {'a': 1}), 42)
        self.assertTrue(executed[1][0].startswith('INSERT INTO scenarios'))
        self.assertEqual(executed[1][1][:5], (7, 'live', 'h' * 64, 'live-gateway-v1', 'live'))
        for bad in (('x' * 101, {}), ('live', {'content': 'text'}), ('live', 'not a dict')):
            with self.assertRaises(ValueError):
                live.ensure_live_scenario(7, bad[0], 'h', 'live', bad[1])

    def test_inner_config_differs_from_the_demo_configs_only_in_upstream_and_pin(self):
        import yaml
        for mode, path in gateway.DEMO_CONFIGS.items():
            demo = yaml.safe_load(path.read_text(encoding='utf-8'))
            kmu = gateway.inner_config(mode, 'kmu', KMU_URL, DEMO_MODEL, DEMO_MODEL)
            self.assertEqual({k: v for k, v in kmu.items() if k != 'upstream'},
                             {k: v for k, v in demo.items() if k != 'upstream'})
            other = gateway.inner_config(mode, 'openai', 'https://api.openai.com/v1', 'gpt-x', DEMO_MODEL)
            self.assertEqual((other['pinned_model'], 'run_conditions' in other), ('gpt-x', False))
            self.assertEqual(other['criteria_parameters'], demo['criteria_parameters'])
            self.assertEqual(other['guardrail_mode'] if 'guardrail_mode' in other else 'observe', mode)


if __name__ == '__main__':
    unittest.main()
