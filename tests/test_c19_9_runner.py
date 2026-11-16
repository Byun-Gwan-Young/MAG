"""C-19-9 part 2 : the runner and the batch runner reach the service gateway through settings only
(GUARDRAIL_GATEWAY_KEY ; --proxy-url / --observe-proxy / --block-proxy). Without the variable nothing changes.
Offline : the real runner against the gateway app, a fake upstream and the fake DB of test_c19_9_gateway."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import httpx

from experiment_config import GATEWAY_KEY_ENV, gateway_headers
from experiments.step_execution import PROTOCOL_V10
from service import gateway
from tools import run_batch
import test_harness_facts as facts
import test_tool_recovery as recovery
from test_c19_9_gateway import DEMO_MODEL, KMU_URL, FakeLive, fake_accounts

ROOT = Path(__file__).resolve().parents[1]
A1V4 = ROOT / 'scenarios/A-1-v4-ambiguous-task-query.yaml'


class HeaderTests(unittest.TestCase):
    def test_no_variable_no_header(self):
        self.assertEqual(gateway_headers({}), {})
        self.assertEqual(gateway_headers({GATEWAY_KEY_ENV: ''}), {})
        self.assertEqual(gateway_headers({GATEWAY_KEY_ENV: 'gk_abcdefgh_x'}), {'X-Guardrail-Key': 'gk_abcdefgh_x'})

    def test_batch_conditions_read_sends_the_key_only_when_set(self):
        seen = []

        def get(url, **kwargs):
            seen.append(kwargs.get('headers'))
            return mock.Mock(json=lambda: {}, raise_for_status=lambda: None)
        with mock.patch('httpx.get', get):
            with mock.patch.dict(os.environ, {GATEWAY_KEY_ENV: ''}):
                run_batch.proxy_conditions('http://127.0.0.1:8000')
            with mock.patch.dict(os.environ, {GATEWAY_KEY_ENV: 'gk_abcdefgh_x'}):
                run_batch.proxy_conditions('https://mag.example')
        self.assertEqual(seen, [{}, {'X-Guardrail-Key': 'gk_abcdefgh_x'}])


class RunnerThroughGatewayTests(unittest.IsolatedAsyncioTestCase):
    """The real A-1-v4 runner in demo phase against a block-mode kmu project : STOP at step 6, refused at step 7 —
    the same result as through the Mac block proxy (test_block_mode.BlockRunTests)."""

    async def test_demo_run_through_the_gateway(self):
        import experiments.run_scenario as runner
        from artifact_hook import capture_artifact
        from scenario_schema import load_scenario
        self.enterContext(mock.patch.dict(os.environ, {'GUARDRAIL_PROVIDER_KMU_URL': KMU_URL}))
        scenario = load_scenario(A1V4).model_copy(deep=True)
        scenario.termination.max_steps = 8
        scenario.model = DEMO_MODEL
        scenario.criteria_version = 'deadlock-v0.3'
        responses = [r for _ in range(8) for r in (recovery.success(), facts.final())]
        upstream = []

        def upstream_handler(request):
            upstream.append(request)
            return responses.pop(0)
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        db = FakeLive()
        app = gateway.create_app(accounts=fake_accounts(), live=db, transport=httpx.MockTransport(upstream_handler),
                                 workdir=root / 'gw')
        real_client = httpx.AsyncClient

        class OfflineClient(real_client):
            def __init__(self, *a, **kw):
                kw.setdefault('transport', httpx.ASGITransport(app=app))
                super().__init__(*a, **kw)

        def acceptance(work):
            artifact = capture_artifact(work.root, scenario.monitored_paths,
                                        test_suite_id=scenario.success_criteria.test_suite.test_suite_id)
            artifact.update(test_pass_count=7, test_total_count=16)
            return {'summary': {'passed': 7, 'tests': 16}, 'gate': {'machine_success': False},
                    'artifact': artifact, 'error': None}
        demo_root = root / 'runs' / 'demo'
        args = SimpleNamespace(scenario=A1V4, run_index=1, temperature=0.0, output_root=demo_root,
                               no_context_buffer=False, context_buffer_messages=12, seed_probe=None,
                               proxy_url='https://mag.example/v1', collection_policy=facts.INDEPENDENT,
                               output_contract=facts.V2, runner_protocol=PROTOCOL_V10, api_key_env='KMU_API_KEY',
                               phase='demo', dataset_run=False)
        clean = {'git_commit': '0' * 40, 'git_dirty': False, 'git_state_status': 'measured', 'git_state_reason': None}
        with mock.patch.object(runner, 'load_scenario', return_value=scenario), \
                mock.patch.object(runner, 'DEMO_OUTPUT_ROOT', demo_root), \
                mock.patch.object(runner.Workspace, 'acceptance', acceptance), \
                mock.patch.object(runner, 'git_state', return_value=clean), \
                mock.patch('httpx.AsyncClient', OfflineClient), \
                mock.patch.dict('os.environ', {'KMU_API_KEY': 'fake-kmu-key', GATEWAY_KEY_ENV: 'gk_kmublk00_x'}), \
                contextlib.redirect_stdout(io.StringIO()):
            code = await runner.run(args)
        out = next(p for p in demo_root.iterdir())
        summary = json.loads((out / 'summary.json').read_text(encoding='utf-8'))
        session = json.loads((out / 'session.json').read_text(encoding='utf-8'))
        self.assertEqual((code, summary['termination_reason'], summary['error']), (0, 'guardrail_stop', None))
        self.assertEqual((summary['guardrail_stop']['stop_step'], summary['guardrail_stop']['blocked_step']), (6, 7))
        self.assertEqual(summary['total_tokens'], sum(t['tokens'] for t in session['turns']))
        self.assertEqual(len(upstream), 12)
        self.assertEqual({r.headers['authorization'] for r in upstream}, {'Bearer fake-kmu-key'})
        self.assertEqual(str(upstream[0].url), KMU_URL + '/chat/completions/')
        # The DB has the same session : 13 calls (the 13th refused with 403), 6 judged steps, STOP at 6, ended.
        db_id = 'p12-' + out.name
        self.assertEqual([c[0] for c in db.named('start_live_session')], [12])
        self.assertEqual(db.named('start_live_session')[0][1], db_id)
        self.assertEqual(db.named('start_live_session')[0][2], scenario.scenario_id)
        calls = db.named('add_llm_call')
        self.assertEqual((len(calls), calls[-1][8]), (13, 403))
        self.assertEqual([j[3] for j in db.named('add_judgement')][-1], 'deadlock')
        (end,) = db.named('end_live_session')
        self.assertEqual((end[0], end[1], end[2], end[4]), (db_id, 'guardrail_stop', 6, 6))
        stored = json.dumps(db.calls, default=str)
        for secret in ('fake-kmu-key', 'gk_kmublk00_x'):
            self.assertNotIn(secret, stored)
        # The key never reaches the run folder (conditions, steps, session).
        for path in out.rglob('*.json'):
            self.assertNotIn('gk_kmublk00_x', path.read_text(encoding='utf-8'), path)


if __name__ == '__main__':
    unittest.main()
