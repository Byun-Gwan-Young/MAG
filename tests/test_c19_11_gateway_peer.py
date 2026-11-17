"""C-19-11 part 2b (DM-122 ②) : the gateway in two slots (service/gateway.py). Offline : two in-process gateways on
one fake DB, the "other slot" reached through an ASGI transport instead of a port.

Checked : a session the old slot holds goes on through the new slot (same judgement state, the request is handed over
after the new slot's own key check and checked again by the old one), a new session stays in the new slot, a handed
request is never handed back, the old refusals (409, 404) are unchanged when the other slot is gone, the LLM key
reaches the other slot unchanged and no log line carries it, and /healthz tells the drain what is still open.
"""
import contextlib
import io
import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import httpx

from service import gateway
from test_c19_9_gateway import DEMO_MODEL, KMU_URL, SECRET, FakeLive, fake_accounts, ok
from test_criteria_v03 import artifact_obs


def refused(request):
    raise httpx.ConnectError('nothing listens', request=request)


class TwoSlots(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(mock.patch.dict(os.environ, {'GUARDRAIL_PROVIDER_KMU_URL': KMU_URL}))
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.upstream, self.live = [], FakeLive()

        def upstream(request):
            self.upstream.append(request)
            return ok(request)
        self.transport = httpx.MockTransport(upstream)
        self.old = gateway.create_app(accounts=fake_accounts(), live=self.live, transport=self.transport,
                                      workdir=self.directory / 'old')
        self.to_old = []

        async def to_old(request):
            self.to_old.append(request)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.old),
                                         base_url='http://old') as client:
                response = await client.request(request.method, request.url.path, headers=request.headers,
                                                content=request.content)
            return httpx.Response(response.status_code, headers=response.headers, content=response.content)
        self.new = self.slot('new', httpx.MockTransport(to_old))
        self.old_client = self.client(self.old)
        self.new_client = self.client(self.new)

    def slot(self, name, peer_transport):
        return gateway.create_app(accounts=fake_accounts(), live=self.live, transport=self.transport,
                                  workdir=self.directory / name, peer='http://127.0.0.1:8050',
                                  peer_transport=peer_transport)

    def client(self, app):
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://mag.test')
        self.addAsyncCleanup(client.aclose)
        return client

    def headers(self, session='s1', key='gk_kmuobs00_x', **extra):
        return {'X-Guardrail-Key': key, 'X-Guardrail-Session': session, 'Authorization': 'Bearer ' + SECRET, **extra}

    async def step(self, client, headers, step):
        chat = await client.post('/v1/chat/completions', headers=headers,
                                 json={'model': DEMO_MODEL, 'messages': [{'role': 'user', 'content': 'hi'}]})
        self.assertEqual(chat.status_code, 200, chat.text)
        response = await client.post('/sessions/observations', headers=headers,
                                     json=artifact_obs(step, {'code.py': 'same'}))
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['observation']

    async def test_a_session_of_the_old_slot_goes_on_through_the_new_slot_with_its_count(self):
        headers = self.headers()
        for step in (1, 2, 3):                                   # before the switch : Caddy -> old slot
            before = await self.step(self.old_client, headers, step)
        for step in (4, 5, 6):                                   # after the switch : Caddy -> new slot -> old slot
            after = await self.step(self.new_client, headers, step)
        # One uninterrupted session : the same store kept counting (a restart would have refused step 4 with 409).
        alone = gateway.create_app(accounts=fake_accounts(), live=FakeLive(), transport=self.transport,
                                   workdir=self.directory / 'alone')
        for step in range(1, 7):
            expected = await self.step(self.client(alone), headers, step)
        self.assertEqual(after['criteria'], expected['criteria'])
        self.assertGreater(after['criteria']['consecutive_steps'], before['criteria']['consecutive_steps'])
        self.assertEqual(len(self.live.named('add_judgement')), 6)
        self.assertEqual({r.headers['x-guardrail-peer'] for r in self.to_old}, {'1'})
        self.assertEqual({r.headers['authorization'] for r in self.to_old}, {'Bearer ' + SECRET})
        self.assertEqual(self.new.state.gateway.routes, {})      # the new slot never owned it
        end = await self.new_client.post('/sessions/end', headers=headers, json={})
        self.assertEqual(end.status_code, 200, end.text)
        log = await self.new_client.get('/log', headers=headers)
        self.assertEqual(log.status_code, 200)

    async def test_a_new_session_stays_in_the_new_slot(self):
        await self.step(self.new_client, self.headers('fresh'), 1)
        self.assertEqual(self.to_old, [])
        self.assertIn((11, 'fresh'), self.new.state.gateway.routes)
        self.assertEqual(self.old.state.gateway.routes, {})

    async def test_the_key_is_checked_before_anything_is_handed_over(self):
        await self.step(self.old_client, self.headers(), 1)
        for key in ('gk_revoked0_x', None):
            headers = self.headers(key=key or 'x')
            if key is None:
                headers.pop('X-Guardrail-Key')
            response = await self.new_client.post('/sessions/observations', headers=headers,
                                                  json=artifact_obs(2, {'code.py': 'same'}))
            self.assertEqual(response.status_code, 401)
        # Another project's key : its own (unknown) session, never the first project's.
        other = await self.new_client.post('/sessions/observations', headers=self.headers(key='gk_kmublk00_x'),
                                           json=artifact_obs(2, {'code.py': 'same'}))
        self.assertEqual(other.status_code, 404)
        self.assertEqual(len(self.live.named('add_judgement')), 1)

    async def test_a_handed_request_is_never_handed_back(self):
        # Both slots point at each other ; a session neither holds must end in one hop.
        hops = []

        async def to_new(request):
            hops.append(request)
            return httpx.Response(599)
        old = self.slot('old-with-peer', httpx.MockTransport(to_new))
        self.live.sessions['p11-gone'] = {}
        response = await self.client(old).post(
            '/v1/chat/completions', headers=self.headers('gone', **{'X-Guardrail-Peer': '1'}),
            json={'model': DEMO_MODEL, 'messages': [{'role': 'user', 'content': 'hi'}]})
        self.assertEqual((response.status_code, response.json()['error']['code']),
                         (409, 'session_recorded_before_restart'))
        unknown = await self.client(old).post('/sessions/end', headers=self.headers('none', **{'X-Guardrail-Peer': '1'}),
                                              json={})
        self.assertEqual((unknown.status_code, unknown.json()), (404, {'detail': 'Unknown session'}))
        self.assertEqual(hops, [])

    async def test_with_the_other_slot_gone_the_refusals_are_the_old_ones(self):
        alone = self.client(self.slot('alone', httpx.MockTransport(refused)))
        self.live.sessions['p11-old'] = {}
        response = await alone.post('/v1/chat/completions', headers=self.headers('old'),
                                    json={'model': DEMO_MODEL, 'messages': [{'role': 'user', 'content': 'hi'}]})
        self.assertEqual((response.status_code, response.json()['error']['code']),
                         (409, 'session_recorded_before_restart'))
        unknown = await alone.post('/sessions/observations', headers=self.headers('nobody'),
                                   json=artifact_obs(1, {'code.py': 'same'}))
        self.assertEqual((unknown.status_code, unknown.json()), (404, {'detail': 'Unknown session'}))
        self.assertEqual(self.upstream, [])

    async def test_an_other_slot_that_breaks_mid_request_is_502_and_no_key_is_logged(self):
        def broken(request):
            raise httpx.ReadError('Bearer ' + SECRET, request=request)
        client = self.client(self.slot('broken', httpx.MockTransport(broken)))
        self.live.sessions['p11-old'] = {}
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        with contextlib.redirect_stderr(stream):
            response = await client.post('/v1/chat/completions', headers=self.headers('old'),
                                         json={'model': DEMO_MODEL, 'messages': [{'role': 'user', 'content': 'hi'}]})
        self.assertEqual((response.status_code, response.json()['error']['code']), (502, 'other_slot_unavailable'))
        self.assertNotIn(SECRET, stream.getvalue() + response.text)
        self.assertNotIn('gk_kmuobs00_x', stream.getvalue() + response.text)

    async def test_healthz_tells_the_drain_what_is_still_open(self):
        health = (await self.old_client.get('/healthz')).json()
        self.assertEqual((health['sessions'], health['open_sessions']), (0, 0))
        await self.step(self.old_client, self.headers('one'), 1)
        await self.step(self.old_client, self.headers('two'), 1)
        self.assertEqual((await self.old_client.get('/healthz')).json()['open_sessions'], 2)
        with mock.patch.object(gateway.time, 'monotonic',
                               return_value=self.old.state.gateway.last_request + 901):
            self.assertEqual((await self.old_client.get('/healthz')).json()['idle_seconds'], 901)
        ended = await self.new_client.post('/sessions/end', headers=self.headers('one'), json={})
        self.assertEqual(ended.status_code, 200, ended.text)
        health = (await self.old_client.get('/healthz')).json()
        self.assertEqual((health['sessions'], health['open_sessions']), (2, 1))
        self.assertEqual(set(health), {'status', 'gateway_version', 'inner_proxies', 'sessions', 'open_sessions',
                                       'idle_seconds'})                      # no project, key or address

    def test_the_other_slot_is_the_other_port_on_loopback_only(self):
        self.assertEqual(gateway.PEER_PORTS, {8050: 8051, 8051: 8050})
        created = []
        with mock.patch.object(gateway, 'create_app', side_effect=lambda **kw: created.append(kw)), \
                mock.patch('uvicorn.run'), mock.patch.object(gateway.logging, 'basicConfig'), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(gateway.main(['--port', '8051']), 0)
            self.assertEqual(gateway.main([]), 0)
            self.assertEqual(gateway.main(['--port', '9000']), 0)             # no known other slot : none
            self.assertEqual(gateway.main(['--peer-port', '0']), 0)
            self.assertEqual(gateway.main(['--port', '8050', '--peer-port', '8050']), 2)
        self.assertEqual([c['peer'] for c in created],
                         ['http://127.0.0.1:8050', 'http://127.0.0.1:8051', None, None])


if __name__ == '__main__':
    unittest.main()
