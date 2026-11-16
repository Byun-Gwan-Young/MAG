"""C-19-9 (DM-92, DM-119) : the service gateway — the frozen proxy behind a guardrail key, live sessions in the DB.

  GUARDRAIL_DB_URL=... GUARDRAIL_PROVIDER_KMU_URL=... venv/bin/python -B -m service.gateway [--port 8050]

A separate entry point (DM-92 ⑤). The proxy module (proxy.py) is imported and used as it is : each request is handed to
an in-process proxy app made by `proxy.create_app`, so judgement (deadlock-v0.3, explain-v1), the block-mode 403 body
and the upstream timeout (600 s read, DM-79) are the Mac proxy's own. What the gateway adds in front of it :

- Guardrail key : header X-Guardrail-Key (not X-Guardrail-Session), checked with storage.accounts.verify_api_key (which
  also updates the key's last use). Missing, unknown or revoked = 401. The shared read-only demo project = 403.
- Key -> project -> mode (observe · block) and provider. The upstream is the project's provider address, and only when
  that address is in the server allow-list (storage.accounts.allowed_upstreams : the C-19-8P table, kmu from server
  settings). Nothing in a request body or header changes the upstream (PM : choose in the project only).
- The caller's LLM key (Authorization) goes to that upstream unchanged. It is never stored or logged here : the proxy
  keeps request bodies only, and the DB repository below writes metadata only (storage.live refuses text and keys).
- One in-process proxy per (project, mode, provider, model) : sessions of different projects never share a store, so
  someone else's session is 404, and a session keeps the mode and model it started with (S-12 : another model in the
  same session gets the proxy's own 409). Block mode : every gateway request is sent as X-Guardrail-Phase: demo, so a
  block project's STOP refuses the next request with the Mac proxy's 403 body (DM-7, DM-32).
- Sessions, LLM-call metadata, observations, judgements and STOP sentences go straight to PostgreSQL through
  storage.live (DM-96 ⑤, source "live", the project's scenario rows) — no JSON session files on the server. The DB
  session id is "p<project id>-<session key>" (session ids are unique across projects).
- Request size limit (GUARDRAIL_GATEWAY_MAX_BODY_BYTES, default 8 MiB) : 413.

Limits (reported) : one worker ; sessions live in this process's memory, so a session recorded before a gateway restart
cannot continue (409 : use a new session key) ; DB writes are synchronous inside the request.
"""
import argparse
import asyncio
from dataclasses import dataclass
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
import yaml  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse, Response  # noqa: E402

import proxy  # noqa: E402
from experiment_config import PHASE_HEADER, load_run_conditions  # noqa: E402
from explain_v1 import STOP_DECISIONS  # noqa: E402

logger = logging.getLogger(__name__)

GATEWAY_VERSION = 'service-gateway-v1'
KEY_HEADER = 'X-Guardrail-Key'
SESSION_HEADER = 'X-Guardrail-Session'
MODEL_HEADER = 'X-Guardrail-Model'
SCENARIO_HEADER = 'X-Guardrail-Scenario'
HOST = '127.0.0.1'
PORT = 8050
LOOPBACK = ('127.0.0.1', 'localhost', '::1')
MAX_BODY_ENV = 'GUARDRAIL_GATEWAY_MAX_BODY_BYTES'
DEFAULT_MAX_BODY = 8 * 1024 * 1024
# The demo proxy configurations : the gateway's judgement settings are theirs, byte for byte (only the upstream and
# the pinned model differ, set per inner proxy below).
DEMO_CONFIGS = {'observe': ROOT / 'guardrail-deadlock-v0.3-kmu.yaml',
                'block': ROOT / 'guardrail-deadlock-v0.3-kmu-block.yaml'}
DEMO_RUN_CONDITIONS = 'demo-conditions.yaml'
# The school gateway documents the trailing-slash chat path (the demo configurations use it ; a redirect breaks POST).
CHAT_PATHS = {'kmu': '/chat/completions/'}
DEFAULT_CHAT_PATH = '/chat/completions'
# Headers handed to the inner proxy besides the session key and the phase (the run labels the runner sends).
PASSED_HEADERS = ('authorization', 'content-type', 'x-guardrail-scenario', 'x-guardrail-run-index',
                  'x-guardrail-framework-version', 'x-guardrail-context-buffer-messages')
RETURNED_HEADERS = ('x-guardrail-session', 'x-guardrail-turn', 'x-should-retry')
SESSION_KEY = re.compile(r'[A-Za-z0-9_-]{1,128}')
SCENARIO_KEY = re.compile(r'[A-Za-z0-9._-]{1,100}')
MODEL_NAME = re.compile(r'[A-Za-z0-9._:/@+-]{1,200}')
LIVE_SCENARIO = 'live'


def error(status, code, message, **extra):
    """The gateway's own refusals, in the proxy's error shape ({"error": {type, code, message}})."""
    return JSONResponse({'error': {'type': 'guardrail_gateway', 'code': code, 'message': message, **extra}},
                        status_code=status, headers={'x-should-retry': 'false'})


def unknown_session():
    # The proxy's own 404 body for a session it does not have (also someone else's session : not told apart).
    return JSONResponse({'detail': 'Unknown session'}, status_code=404)


def demo_model(root=ROOT):
    return load_run_conditions(DEMO_RUN_CONDITIONS, root)['model']


def inner_config(mode, provider, base_url, model, pinned_demo_model):
    """The inner proxy's configuration : the demo configuration of this mode with the project's upstream. The demo
    model on kmu keeps run_conditions (the Mac demo proxy's pin, so the runner and the batch runner see the same
    pin record) ; any other model is pinned by name (S-12 : one inner proxy = one model)."""
    data = yaml.safe_load(DEMO_CONFIGS[mode].read_text(encoding='utf-8'))
    data['upstream'] = {'base_url': base_url, 'chat_path': CHAT_PATHS.get(provider, DEFAULT_CHAT_PATH),
                        # A name only (the runner checks it against its own) ; the proxy never reads its value.
                        'api_key_env': provider.upper().replace('-', '_') + '_API_KEY'}
    if not (provider == 'kmu' and model == pinned_demo_model):
        data.pop('run_conditions', None)
        data['pinned_model'] = model
    return data


def observation_data(observation):
    """The judge input of one observation for storage.live, in the raw-run importer's shape (import_utils.
    observation_input), without the paired extraction and evidence quotes (text read from utterances)."""
    from storage.import_utils import observation_input
    data = observation_input(observation)
    data.pop('extracted_conversation', None)
    conversation = data.get('conversation')
    if isinstance(conversation, dict) and conversation.get('evidence'):
        data['conversation'] = {**conversation, 'evidence': []}
    return data


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else None


class LiveRepository:
    """The inner proxy's session repository (session_store.SessionRepository) on storage.live.

    create : register the declared scenario and start the live session. save : write what is new since the last save —
    LLM-call metadata per turn, then each new step's observation and judgement, then the end. Only metadata and the
    structured judge input leave this class ; request and response bodies stay in the proxy's memory.
    """

    def __init__(self, live, project_id, provider):
        self.live, self.project_id, self.provider = live, project_id, provider
        self.declared = {}   # session key -> scenario key the caller declared (X-Guardrail-Scenario)
        self.written = {}    # session key -> {'turns', 'observations', 'ended'} already in the DB

    def db_id(self, key):
        return f'p{self.project_id}-{key}'

    def create(self, record):
        key, meta = record['session_id'], record['meta']
        scenario_key = self.declared.pop(key, None) or LIVE_SCENARIO
        config = {'gateway_version': GATEWAY_VERSION, 'scenario_key': scenario_key,
                  'registration': meta.get('registration')}
        scenario_hash = hashlib.sha256(json.dumps(config, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        pin = meta.get('pinned_model') or {}
        execution_config = {'gateway_version': GATEWAY_VERSION, 'provider': self.provider,
                            'criteria_version': meta.get('criteria_version'),
                            'criteria_sha256': meta.get('criteria_sha256'),
                            'criteria_freeze_sha256': (meta.get('criteria_freeze') or {}).get('sha256'),
                            'pinned_model': pin, 'phase': proxy.DEMO_PHASE}
        try:
            self.live.ensure_live_scenario(self.project_id, scenario_key, scenario_hash, scenario_key, config)
            self.live.start_live_session(self.project_id, self.db_id(key), scenario_key, scenario_hash,
                                         meta.get('guardrail_mode'), pin.get('name'), execution_config)
        except ValueError as exc:
            if 'already exists' in str(exc):
                raise FileExistsError(self.db_id(key)) from None
            raise OSError(f'live session start not recorded ({type(exc).__name__})') from None
        except Exception as exc:  # DB down, project gone : the proxy forwards without recording (its own rule)
            raise OSError(f'live session start not recorded ({type(exc).__name__})') from None
        self.written[key] = {'turns': 0, 'observations': 0, 'ended': False}

    def load(self, session_id):
        # The gateway routes only sessions it started in this process (a recorded one is refused before this).
        return None

    def active_ids(self):
        return set()

    def save(self, record):
        key = record['session_id']
        state = self.written.get(key)
        if state is None:
            return
        db_id, meta = self.db_id(key), record['meta']
        turns, observations = record['turns'], record['observations']
        steps_of = {t: o['step_id'] for o in observations for t in o.get('llm_turns') or []}
        for turn in turns[state['turns']:]:
            usage = ((turn.get('response_body') or {}).get('usage') or {}) if isinstance(
                turn.get('response_body'), dict) else {}
            details = usage.get('completion_tokens_details') if isinstance(usage, dict) else None
            reasoning = details.get('reasoning_tokens') if isinstance(details, dict) else None
            self.live.add_llm_call(db_id, turn['turn'], steps_of.get(turn['turn'], len(observations) + 1), None,
                                   _count(turn.get('prompt_tokens')), _count(turn.get('completion_tokens')),
                                   _count(reasoning), _number(turn.get('upstream_ms')), _count(turn.get('status_code')),
                                   _count(turn.get('retry_count')), _count(turn.get('tool_calls')))
            state['turns'] += 1
        for observation in observations[state['observations']:]:
            step, agent = observation['step_id'], observation.get('agent_id')
            criteria = observation.get('criteria') or {}
            progress = observation.get('progress') or {}
            self.live.add_observation(db_id, step, agent, observation_data(observation))
            self.live.add_judgement(
                db_id, step, agent, criteria.get('decision') or 'unmeasured',
                criteria.get('criteria_version') or meta.get('criteria_version'), progress.get('state'),
                _count(criteria.get('consecutive_steps')),
                criteria.get('reason') if isinstance(criteria.get('reason'), str) else None,
                _count(observation.get('judgement_ns')),
                {'collection': observation.get('collection'), 'progress': progress, 'criteria': criteria})
            state['observations'] += 1
        if record['status'] == 'ended' and not state['ended']:
            tokens = [t.get('tokens') for t in turns]
            first_stop = next((o['step_id'] for o in observations
                               if (o.get('criteria') or {}).get('decision') in STOP_DECISIONS), None)
            self.live.end_live_session(db_id, meta.get('termination_reason'), len(observations),
                                       sum(tokens) if all(_count(t) is not None for t in tokens) else None,
                                       first_stop)
            state['ended'] = True


@dataclass
class Inner:
    app: object
    repository: LiveRepository
    client: httpx.AsyncClient


class Gateway:
    def __init__(self, accounts, live, transport=None, workdir=None, max_body=DEFAULT_MAX_BODY, root=ROOT):
        self.accounts, self.live, self.transport, self.max_body = accounts, live, transport, max_body
        self.workdir = Path(workdir or tempfile.mkdtemp(prefix='mag-gateway-'))
        self.demo_model = demo_model(root)
        self.inner = {}    # (project id, mode, provider, base URL, model) -> Inner
        self.routes = {}   # (project id, session key) -> inner key

    # --- the inner proxies ---

    def inner_app(self, inner_key):
        found = self.inner.get(inner_key)
        if found is not None:
            return found
        project_id, mode, provider, base_url, model = inner_key
        folder = self.workdir / str(len(self.inner) + 1)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f'gateway-{provider}-{mode}.yaml'
        path.write_text(yaml.safe_dump(inner_config(mode, provider, base_url, model, self.demo_model),
                                       allow_unicode=True, sort_keys=False), encoding='utf-8')
        repository = LiveRepository(self.live, project_id, provider)
        app = proxy.create_app(repository=repository, transport=self.transport, config_path=path)
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                                   base_url='http://inner-proxy', timeout=None)
        found = self.inner[inner_key] = Inner(app, repository, client)
        return found

    async def forward(self, inner, method, path, request, key, body=b''):
        headers = {k: v for k, v in request.headers.items() if k.lower() in PASSED_HEADERS}
        headers[SESSION_HEADER] = key
        # Every gateway session is a service (demo-condition) session : block mode acts on it (DM-7, DM-32).
        headers[PHASE_HEADER] = proxy.DEMO_PHASE
        response = await inner.client.request(method, path, headers=headers, content=body)
        return response

    @staticmethod
    def relay(response):
        headers = {k: v for k, v in response.headers.items() if k.lower() in RETURNED_HEADERS}
        return Response(content=response.content, status_code=response.status_code, headers=headers,
                        media_type=response.headers.get('content-type'))

    # --- checks in front of the proxy ---

    async def authenticate(self, request):
        """(project, provider, base URL) for the request's guardrail key, or an error response."""
        raw = request.headers.get(KEY_HEADER)
        if not raw:
            return error(401, 'guardrail_key_required', KEY_HEADER + ' 헤더에 MAG API 키가 필요합니다')
        try:
            project = await asyncio.to_thread(self.accounts.verify_api_key, raw)
        except Exception as exc:
            logger.warning('Key check unavailable (%s)', type(exc).__name__)
            return error(503, 'key_check_unavailable', '키 확인을 할 수 없습니다. 잠시 뒤 다시 시도하세요')
        if project is None:
            return error(401, 'guardrail_key_invalid', 'MAG API 키가 없거나 폐기되었습니다')
        if project.get('read_only'):
            return error(403, 'project_read_only', '데모 프로젝트는 읽기 전용입니다')
        if project.get('mode') not in DEMO_CONFIGS:
            return error(503, 'project_mode_unknown', '프로젝트 모드를 알 수 없습니다')
        base_url = (project.get('upstream_url') or '').rstrip('/')
        provider = self.accounts.allowed_upstreams().get(base_url)
        if provider is None:
            return error(403, 'upstream_not_allowed',
                         '이 프로젝트의 LLM 제공자 주소가 서버 허용 목록에 없습니다. 프로젝트에서 제공자를 다시 고르세요')
        return project, provider, base_url

    async def read_body(self, request):
        length = request.headers.get('content-length')
        if length is not None and length.isdigit() and int(length) > self.max_body:
            return None
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > self.max_body:
                return None
        return bytes(body)

    def too_large(self):
        return error(413, 'request_too_large', f'요청 본문이 상한 {self.max_body} 바이트를 넘습니다')

    async def recorded(self, project_id, key):
        """Whether the DB already has this session (started before this process) ; None when it cannot be read."""
        try:
            await asyncio.to_thread(self.live.get_session_progress, f'p{project_id}-{key}', 0)
            return True
        except LookupError:
            return False
        except Exception as exc:
            logger.warning('Live session lookup unavailable (%s)', type(exc).__name__)
            return None

    async def open_route(self, project, provider, base_url, key, model, scenario):
        """The inner proxy for a session : its route if it has one, else a new route (after the DB check)."""
        route = (project['id'], key)
        inner_key = self.routes.get(route)
        if inner_key is None:
            known = await self.recorded(project['id'], key)
            if known is None:
                return error(503, 'session_store_unavailable', '세션 기록 DB 에 연결할 수 없습니다')
            if known:
                return error(409, 'session_recorded_before_restart',
                             '이 세션 키는 게이트웨이 재시작 전에 기록되었습니다. 새 세션 키를 쓰세요')
            inner_key = self.routes.setdefault(route, (project['id'], project['mode'], provider, base_url, model))
        inner = self.inner_app(inner_key)
        if scenario is not None and key not in inner.repository.written:
            inner.repository.declared[key] = scenario
        return inner

    def routed(self, project, key):
        inner_key = self.routes.get((project['id'], key))
        return None if inner_key is None else self.inner_app(inner_key)


def session_header(request):
    """The X-Guardrail-Session value, '' when missing, None when malformed (the proxy's own rule)."""
    value = request.headers.get(SESSION_HEADER)
    if not value:
        return ''
    if not SESSION_KEY.fullmatch(value) or re.fullmatch(r'(?i:CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])', value):
        return None
    return value


def scenario_header(request):
    value = request.headers.get(SCENARIO_HEADER)
    if value is None:
        return None, None
    if not SCENARIO_KEY.fullmatch(value):
        return None, error(400, 'invalid_scenario', SCENARIO_HEADER + ' 는 1~100 자의 영문 · 숫자 · . _ - 만')
    return value, None


def model_name(value):
    return value if isinstance(value, str) and MODEL_NAME.fullmatch(value) else None


def create_app(*, accounts=None, live=None, transport=None, workdir=None, max_body=None, root=ROOT):
    if accounts is None:
        from storage import accounts
    if live is None:
        from storage import live
    if max_body is None:
        max_body = int(os.environ.get(MAX_BODY_ENV, DEFAULT_MAX_BODY))
    gateway = Gateway(accounts, live, transport, workdir, max_body, root)
    app = FastAPI(title='MAG service gateway (Multi-Agent Guardrail)', version=GATEWAY_VERSION,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.gateway = gateway

    async def start(request):
        """(project, provider, base URL, session key, body) for a request that names its session, or an error."""
        checked = await gateway.authenticate(request)
        if isinstance(checked, Response):
            return checked
        key = session_header(request)
        if key is None:
            return error(400, 'invalid_session_key', SESSION_HEADER + ' 는 1~128 자의 영문 · 숫자 · _ - 만')
        body = await gateway.read_body(request)
        if body is None:
            return gateway.too_large()
        return (*checked, key, body)

    @app.post('/v1/chat/completions')
    async def chat(request: Request):
        started = await start(request)
        if isinstance(started, Response):
            return started
        project, provider, base_url, key, raw = started
        try:
            body = json.loads(raw)
        except ValueError:
            return error(400, 'invalid_json', '요청 본문이 JSON 이 아닙니다')
        if not isinstance(body, dict):
            return error(400, 'invalid_json', '요청 본문은 JSON 객체여야 합니다')
        model = model_name(body.get('model'))
        if model is None:
            return error(400, 'model_required', '요청 본문의 model 이 필요합니다')
        if not key:
            messages = body.get('messages')
            if not (isinstance(messages, list) and messages and messages[0]):
                return error(400, 'session_key_required',
                             SESSION_HEADER + ' 헤더가 없고 첫 메시지도 없어 세션을 정할 수 없습니다')
            key = 'hash-' + hashlib.sha256(json.dumps(messages[0], sort_keys=True, ensure_ascii=False)
                                           .encode()).hexdigest()   # the proxy's own key for this case
        scenario, problem = scenario_header(request)
        if problem is not None:
            return problem
        inner = await gateway.open_route(project, provider, base_url, key, model, scenario)
        if isinstance(inner, Response):
            return inner
        return gateway.relay(await gateway.forward(inner, 'POST', '/v1/chat/completions', request, key, raw))

    @app.post('/sessions')
    async def start_session(request: Request):
        started = await start(request)
        if isinstance(started, Response):
            return started
        project, provider, base_url, key, raw = started
        if not key:
            return error(400, 'session_key_required', SESSION_HEADER + ' 헤더가 필요합니다')
        model = model_name(request.headers.get(MODEL_HEADER))
        if model is None and request.headers.get(MODEL_HEADER) is not None:
            return error(400, 'invalid_model', MODEL_HEADER + ' 모양이 아닙니다')
        if model is None and provider == 'kmu':
            model = gateway.demo_model
        if model is None:
            return error(400, 'model_required', '첫 요청 전에 세션을 시작하려면 ' + MODEL_HEADER + ' 헤더가 필요합니다')
        scenario, problem = scenario_header(request)
        if problem is not None:
            return problem
        new = (project['id'], key) not in gateway.routes
        inner = await gateway.open_route(project, provider, base_url, key, model, scenario)
        if isinstance(inner, Response):
            return inner
        response = await gateway.forward(inner, 'POST', '/sessions', request, key, raw)
        if new and response.is_error:
            gateway.routes.pop((project['id'], key), None)
            inner.repository.declared.pop(key, None)
        return gateway.relay(response)

    async def existing(request, method, path):
        started = await start(request)
        if isinstance(started, Response):
            return started
        project, _, _, key, raw = started
        if not key:
            return error(400, 'session_key_required', SESSION_HEADER + ' 헤더가 필요합니다')
        inner = gateway.routed(project, key)
        if inner is None:
            return unknown_session()
        return gateway.relay(await gateway.forward(inner, method, path, request, key, raw))

    @app.post('/sessions/observations')
    async def observations(request: Request):
        return await existing(request, 'POST', '/sessions/observations')

    @app.post('/sessions/end')
    async def end(request: Request):
        return await existing(request, 'POST', '/sessions/end')

    @app.get('/log')
    async def session_log(request: Request):
        # The proxy's record of the caller's own session, to the holder of its project's key only : the runner keeps
        # it as session.json (turns, token totals) exactly as from the Mac proxy. It lives in this process's memory
        # and is never written to the DB or a file here.
        return await existing(request, 'GET', '/log')

    @app.get('/conditions')
    async def conditions(request: Request):
        checked = await gateway.authenticate(request)
        if isinstance(checked, Response):
            return checked
        project, provider, base_url = checked
        model = model_name(request.headers.get(MODEL_HEADER)) or (gateway.demo_model if provider == 'kmu' else None)
        if model is None:
            return error(400, 'model_required', MODEL_HEADER + ' 헤더가 필요합니다')
        inner = gateway.inner_app((project['id'], project['mode'], provider, base_url, model))
        response = await inner.client.get('/conditions')
        return gateway.relay(response)

    @app.get('/healthz')
    async def healthz():
        # Local only (Caddy does not route it) : no project, key or address in it.
        return {'status': 'ok', 'gateway_version': GATEWAY_VERSION, 'inner_proxies': len(gateway.inner),
                'sessions': len(gateway.routes)}

    return app


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--host', default=HOST)
    parser.add_argument('--port', type=int, default=PORT)
    args = parser.parse_args(argv)
    if args.host not in LOOPBACK:
        print(json.dumps({'error': f'refusing to bind {args.host!r}: Caddy fronts the gateway, loopback only'}))
        return 2
    logging.basicConfig(level=logging.INFO)
    import uvicorn
    uvicorn.run(create_app(), host=args.host, port=args.port, workers=1, log_level='info')
    return 0


if __name__ == '__main__':
    sys.exit(main())
