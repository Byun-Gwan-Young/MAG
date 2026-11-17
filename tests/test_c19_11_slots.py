"""C-19-11 part 2 (DM-119, DM-122) : the two-slot switch on the server (deploy/aws/slots.sh), offline.

The script runs under /bin/bash against a temporary folder that stands in for / (GUARDRAIL_PREFIX), with fake
systemctl, systemd-run, curl, caddy, journalctl, sleep and uv first on PATH. No server, no network, no root.
Checked : which slot the next version goes to, the order (health before Caddy), that a failed health check, a failed
Caddyfile check or a failed check through Caddy leaves the version in use as it was and ends with 1, the old slot's
clean-up (the gateway is drained, not stopped), the drain's three ends, rollback to a kept release, and which releases
are kept.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
AWS = ROOT / 'deploy' / 'aws'
DOMAIN = 'demo.example.org'

FAKE = textwrap.dedent('''\
    #!{python}
    import json, os, sys
    from pathlib import Path
    name, args = Path(sys.argv[0]).name, sys.argv[1:]
    state = Path(os.environ['FAKE_STATE'])
    with open(state / 'calls.log', 'a') as log:
        log.write(json.dumps([name, *args]) + '\\n')
    active_file = state / 'active'
    active = set(active_file.read_text().split()) if active_file.exists() else set()
    fail = (state / 'fail').read_text().split('\\n') if (state / 'fail').exists() else []
    line = ' '.join([name, *args])
    if any(f and f in line for f in fail):
        sys.exit(1)
    if name == 'systemctl':
        words = [a for a in args if not a.startswith('-')]
        verb, units = words[0], words[1:]
        if verb == 'is-active':
            ok = all(u in active for u in units)
            if '--quiet' not in args:
                print('active' if ok else 'inactive')
            sys.exit(0 if ok else 3)
        if verb in ('restart', 'start', 'reload-or-restart'):
            active |= set(units)
        elif verb == 'stop':
            active -= set(units)
        active_file.write_text('\\n'.join(sorted(active)))
    elif name == 'curl':
        body = state / 'healthz.json'
        if '-o' not in args and body.exists():
            print(body.read_text())
''')
FAKES = ('systemctl', 'systemd-run', 'curl', 'caddy', 'journalctl', 'sleep', 'uv')


class Server:
    """A temporary / with releases, and the fakes' state."""

    def __init__(self, tmp):
        self.root = Path(tmp) / 'root'
        self.state = Path(tmp) / 'state'
        self.bin = Path(tmp) / 'bin'
        for folder in (self.root / 'opt/guardrail/releases', self.root / 'etc/guardrail', self.root / 'etc/caddy',
                       self.root / 'etc/systemd/system', self.state, self.bin):
            folder.mkdir(parents=True)
        for name in FAKES:
            (self.bin / name).write_text(FAKE.format(python=sys.executable), encoding='utf-8')
            (self.bin / name).chmod(0o755)
        self.count = 0

    @property
    def caddy_env(self):
        return self.root / 'etc/guardrail/caddy.env'

    def release(self, name=None):
        """A release folder with this repository's deploy/aws (so its slots.sh is the script under test)."""
        self.count += 1
        name = name or f'{self.count:012x}-20261117T0{self.count}0000Z'
        folder = self.root / 'opt/guardrail/releases' / name
        (folder / 'deploy/aws').mkdir(parents=True)
        (folder / 'service').mkdir()
        (folder / 'service/requirements-server.txt').write_text('', encoding='utf-8')
        (folder / 'DEPLOY_INFO.json').write_text('{}', encoding='utf-8')
        for path in AWS.iterdir():
            if path.suffix in ('.sh', '.service', '.template'):
                (folder / 'deploy/aws' / path.name).write_bytes(path.read_bytes())
        os.utime(folder, (1_800_000_000 + self.count, 1_800_000_000 + self.count))
        return folder

    def run(self, release, *args, **env):
        result = subprocess.run(
            ['/bin/bash', str(Path(release) / 'deploy/aws/slots.sh'), *args], capture_output=True, text=True, timeout=60,
            env={'PATH': f'{self.bin}:/usr/bin:/bin', 'LANG': 'C.UTF-8', 'GUARDRAIL_PREFIX': str(self.root),
                 'FAKE_STATE': str(self.state), 'GUARDRAIL_HEALTH_TRIES': '2', **env})
        return result

    def calls(self, name=None):
        log = self.state / 'calls.log'
        calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return [c for c in calls if name is None or c[0] == name]

    def active(self):
        path = self.state / 'active'
        return set(path.read_text().split()) if path.exists() else set()

    def set_active(self, *units):
        (self.state / 'active').write_text('\n'.join(units))

    def fail(self, *patterns):
        (self.state / 'fail').write_text('\n'.join(patterns))

    def link(self, name):
        path = self.root / 'opt/guardrail' / name
        return Path(os.path.realpath(path)).name if path.is_symlink() else None

    def env(self):
        return dict(line.split('=', 1) for line in self.caddy_env.read_text().splitlines() if '=' in line)

    def in_slot(self, slot, release):
        """The server as a finished deploy to `slot` leaves it."""
        ports = {'a': (8020, 8030, 8050), 'b': (8021, 8031, 8051)}[slot]
        self.caddy_env.write_text(
            f'GUARDRAIL_DOMAIN={DOMAIN}\nGUARDRAIL_SLOT={slot}\nGUARDRAIL_API_UPSTREAM=127.0.0.1:{ports[0]}\n'
            f'GUARDRAIL_BACKEND_UPSTREAM=127.0.0.1:{ports[1]}\nGUARDRAIL_GATEWAY_UPSTREAM=127.0.0.1:{ports[2]}\n')
        (self.root / 'opt/guardrail/slots').mkdir(exist_ok=True)
        for link in ('app', f'slots/{slot}'):
            path = self.root / 'opt/guardrail' / link
            if path.is_symlink():
                path.unlink()
            path.symlink_to(release)
        (self.root / 'etc/caddy/Caddyfile').write_text('# in use\n')
        self.set_active('caddy', *(f'guardrail-{s}@{slot}.service' for s in ('api', 'backend', 'gateway')))


def units(slot):
    return {f'guardrail-{s}@{slot}.service' for s in ('api', 'backend', 'gateway')}


class SlotTests(unittest.TestCase):
    def setUp(self):
        self.server = Server(self.enterContext(tempfile.TemporaryDirectory()))

    def pick(self, release):
        result = self.server.run(release, 'pick')
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    # --- which slot ---

    def test_the_next_version_goes_to_the_slot_not_in_use(self):
        release = self.server.release()
        self.assertEqual(self.pick(release), 'a')                      # a server that never served
        self.server.set_active('guardrail-api.service')               # deployed before C-19-11 (single units, slot a's ports)
        self.assertEqual(self.pick(release), 'b')
        self.server.in_slot('a', release)
        self.assertEqual(self.pick(release), 'b')
        self.server.in_slot('b', release)
        self.assertEqual(self.pick(release), 'a')
        self.server.caddy_env.write_text('GUARDRAIL_SLOT=c\n')
        self.assertNotEqual(self.server.run(release, 'pick').returncode, 0)

    # --- a deploy that works ---

    def test_deploy_checks_the_new_slot_then_moves_caddy_then_drains_the_old_gateway(self):
        old, new = self.server.release(), self.server.release()
        self.server.in_slot('a', old)
        self.server.caddy_env.write_text(self.server.caddy_env.read_text() + 'OTHER_LINE=kept\n')
        result = self.server.run(new, 'deploy', str(new), DOMAIN)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.server.env(), {
            'OTHER_LINE': 'kept', 'GUARDRAIL_DOMAIN': DOMAIN, 'GUARDRAIL_SLOT': 'b',
            'GUARDRAIL_API_UPSTREAM': '127.0.0.1:8021', 'GUARDRAIL_BACKEND_UPSTREAM': '127.0.0.1:8031',
            'GUARDRAIL_GATEWAY_UPSTREAM': '127.0.0.1:8051'})
        self.assertEqual(self.server.caddy_env.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.server.link('app'), self.server.link('slots/b'), self.server.link('slots/a')),
                         (new.name, new.name, old.name))
        self.assertEqual((self.server.root / 'etc/caddy/Caddyfile').read_bytes(),
                         (AWS / 'Caddyfile.template').read_bytes())
        self.assertEqual((self.server.root / 'etc/guardrail/slot-b.env').read_text(),
                         'GUARDRAIL_API_PORT=8021\nGUARDRAIL_BACKEND_PORT=8031\nGUARDRAIL_GATEWAY_PORT=8051\n')
        # The old gateway is still running (its sessions) ; only a detached drain was started for it.
        self.assertEqual(self.server.active(), units('b') | {'caddy', 'guardrail-gateway@a.service'})
        drains = self.server.calls('systemd-run')
        self.assertEqual(len(drains), 1)
        self.assertEqual(drains[0][-5:], ['guardrail-drain-a', '/bin/bash', str(new.resolve() / 'deploy/aws/slots.sh'),
                                          'drain', 'a'])
        # Order : the three health checks on the new ports, then Caddy, then the old API · backend stop.
        calls = [' '.join(c) for c in self.server.calls()]
        def at(text):
            return next(i for i, c in enumerate(calls) if text in c)
        order = [at('http://127.0.0.1:8021/api/summary'), at('http://127.0.0.1:8031/healthz'),
                 at('http://127.0.0.1:8051/healthz'), at('caddy validate'), at('caddy reload'),
                 at(f'--resolve {DOMAIN}:443:127.0.0.1 https://{DOMAIN}/api/auth/config'),
                 at('systemctl stop guardrail-api@a.service'), at('systemctl stop guardrail-backend@a.service')]
        self.assertEqual(order, sorted(order))
        self.assertFalse(any('stop guardrail-gateway@a.service' in c for c in calls))
        self.assertFalse(any(c.startswith('curl') and ':8020' in c for c in calls))   # never asks the old slot

    def test_first_deploy_after_the_single_units_goes_to_b_and_removes_them(self):
        release = self.server.release()
        unit_dir = self.server.root / 'etc/systemd/system'
        for name in ('api', 'backend', 'gateway'):
            (unit_dir / f'guardrail-{name}.service').write_text('[Unit]\n')
        self.server.caddy_env.write_text(f'GUARDRAIL_DOMAIN={DOMAIN}\n')
        self.server.set_active('caddy', 'guardrail-api.service', 'guardrail-backend.service', 'guardrail-gateway.service')
        result = self.server.run(release, 'deploy', str(release), DOMAIN)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.server.env()['GUARDRAIL_SLOT'], 'b')
        self.assertEqual(self.server.active(), units('b') | {'caddy', 'guardrail-gateway.service'})
        self.assertEqual(sorted(p.name for p in unit_dir.iterdir()),
                         ['guardrail-api@.service', 'guardrail-backend@.service', 'guardrail-gateway.service',
                          'guardrail-gateway@.service'])
        self.assertEqual(self.server.calls('systemd-run')[0][-2:], ['drain', 'legacy'])

    def test_a_drain_left_on_the_idle_slot_is_ended_before_the_slot_is_used_again(self):
        old, new = self.server.release(), self.server.release()
        self.server.in_slot('b', old)
        self.server.set_active(*self.server.active(), 'guardrail-gateway@a.service', 'guardrail-drain-a.service')
        self.assertEqual(self.server.run(new, 'deploy', str(new), DOMAIN).returncode, 0)
        calls = [' '.join(c) for c in self.server.calls()]
        self.assertLess(calls.index('systemctl stop guardrail-drain-a.service'),
                        calls.index('systemctl restart guardrail-gateway@a.service'))
        self.assertEqual(self.server.env()['GUARDRAIL_SLOT'], 'a')

    # --- a deploy that must not switch ---

    def unchanged(self, old, before_env, result):
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(self.server.caddy_env.read_text(), before_env)
        self.assertEqual((self.server.root / 'etc/caddy/Caddyfile').read_text(), '# in use\n')
        self.assertEqual(self.server.link('app'), old.name)
        self.assertEqual(self.server.active(), units('a') | {'caddy'})          # old slot running, new slot stopped
        calls = [' '.join(c) for c in self.server.calls()]
        self.assertFalse(any(c.startswith('systemctl stop') and '@a.service' in c for c in calls), calls)
        self.assertEqual(self.server.calls('systemd-run'), [])
        self.assertIn('그대로다', result.stderr)

    def test_a_silent_service_in_the_new_slot_never_reaches_caddy(self):
        for silent in ('8021/api/summary', '8031/healthz', '8051/healthz'):
            with self.subTest(silent=silent), tempfile.TemporaryDirectory() as tmp:
                self.server = Server(tmp)
                old, new = self.server.release(), self.server.release()
                self.server.in_slot('a', old)
                before = self.server.caddy_env.read_text()
                self.server.fail(silent)
                result = self.server.run(new, 'deploy', str(new), DOMAIN)
                self.unchanged(old, before, result)
                self.assertEqual(self.server.calls('caddy'), [])

    def test_a_unit_that_does_not_start_never_reaches_caddy(self):
        old, new = self.server.release(), self.server.release()
        self.server.in_slot('a', old)
        before = self.server.caddy_env.read_text()
        self.server.fail('systemctl restart guardrail-backend@b.service')
        self.unchanged(old, before, self.server.run(new, 'deploy', str(new), DOMAIN))
        self.assertEqual(self.server.calls('caddy'), [])

    def test_a_caddyfile_that_does_not_validate_changes_nothing(self):
        old, new = self.server.release(), self.server.release()
        self.server.in_slot('a', old)
        before = self.server.caddy_env.read_text()
        self.server.fail('caddy validate')
        self.unchanged(old, before, self.server.run(new, 'deploy', str(new), DOMAIN))
        self.assertFalse(any(c[1] == 'reload' for c in self.server.calls('caddy')))
        self.assertEqual(sorted(p.name for p in (self.server.root / 'etc/guardrail').iterdir()),
                         ['caddy.env', 'slot-b.env'])                     # no half-written file left

    def test_no_answer_through_caddy_after_the_reload_puts_everything_back(self):
        old, new = self.server.release(), self.server.release()
        self.server.in_slot('a', old)
        before = self.server.caddy_env.read_text()
        self.server.fail('/api/auth/config')
        self.unchanged(old, before, self.server.run(new, 'deploy', str(new), DOMAIN))
        reloads = [c for c in self.server.calls('caddy') if c[1] == 'reload']
        self.assertEqual(len(reloads), 2)                                 # to the new slot, and back

    def test_deploy_refuses_a_folder_that_is_not_a_release_and_a_bad_domain(self):
        release = self.server.release()
        self.assertEqual(self.server.run(release, 'deploy', str(self.server.root), DOMAIN).returncode, 1)
        self.assertEqual(self.server.run(release, 'deploy', str(release), 'bad domain;rm').returncode, 1)
        self.assertEqual(self.server.calls('systemctl'), [])

    # --- the old gateway ---

    def drain(self, healthz, **env):
        old, new = self.server.release(), self.server.release()
        self.server.in_slot('b', new)
        self.server.set_active(*self.server.active(), 'guardrail-gateway@a.service')
        if healthz is not None:
            (self.server.state / 'healthz.json').write_text(json.dumps(healthz))
        result = self.server.run(new, 'drain', 'a', **env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('guardrail-gateway@a.service', self.server.active())
        self.assertEqual(self.server.active() & units('b'), units('b'))
        return result.stdout, len(self.server.calls('sleep'))

    def test_drain_stops_the_old_gateway_when_no_session_is_open(self):
        out, sleeps = self.drain({'status': 'ok', 'sessions': 4, 'open_sessions': 0, 'idle_seconds': 3})
        self.assertIn('열린 세션 0', out)
        self.assertEqual(sleeps, 0)

    def test_drain_stops_it_when_no_request_came_for_fifteen_minutes(self):
        out, sleeps = self.drain({'status': 'ok', 'sessions': 4, 'open_sessions': 2, 'idle_seconds': 900})
        self.assertIn('요청 없음 900 초', out)
        self.assertEqual(sleeps, 0)

    def test_drain_waits_for_open_sessions_but_never_past_the_limit(self):
        out, sleeps = self.drain({'status': 'ok', 'sessions': 4, 'open_sessions': 2, 'idle_seconds': 10},
                                 GUARDRAIL_DRAIN_MAX_SECONDS='60', GUARDRAIL_DRAIN_POLL_SECONDS='15')
        self.assertIn('상한 60 초', out)
        self.assertEqual(sleeps, 4)

    def test_drain_of_a_gateway_from_before_this_unit_reads_its_session_count(self):
        out, _ = self.drain({'status': 'ok', 'gateway_version': 'service-gateway-v1', 'inner_proxies': 0, 'sessions': 0})
        self.assertIn('열린 세션 0', out)

    def test_drain_never_stops_the_slot_in_use(self):
        release = self.server.release()
        self.server.in_slot('a', release)
        result = self.server.run(release, 'drain', 'a')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.server.active(), units('a') | {'caddy'})

    def test_without_systemd_run_the_old_gateway_is_stopped_at_once(self):
        old, new = self.server.release(), self.server.release()
        self.server.in_slot('a', old)
        self.server.fail('systemd-run')
        result = self.server.run(new, 'deploy', str(new), DOMAIN)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.server.active(), units('b') | {'caddy'})

    # --- rollback and kept releases ---

    def test_rollback_starts_the_previous_release_in_the_idle_slot_without_a_db_load(self):
        first, second, third = self.server.release(), self.server.release(), self.server.release()
        self.server.in_slot('a', third)
        result = self.server.run(third, 'rollback')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.server.link('app'), self.server.link('slots/b'), self.server.env()['GUARDRAIL_SLOT']),
                         (second.name, second.name, 'b'))
        uv = self.server.calls('uv')
        self.assertEqual(uv[0][-2:], ['-r', str(second.resolve() / 'service/requirements-server.txt')])
        self.assertFalse(any('storage' in ' '.join(c) for c in self.server.calls()))
        self.assertTrue(first.is_dir())

    def test_rollback_to_a_named_release_and_its_refusals(self):
        first, second, third = self.server.release(), self.server.release(), self.server.release()
        self.server.in_slot('a', third)
        for bad in ('../../etc', 'nope', third.name, '0123456789ab-20261117T000000Z'):
            result = self.server.run(third, 'rollback', bad)
            self.assertEqual(result.returncode, 1, bad)
        self.assertEqual(self.server.link('app'), third.name)
        self.assertEqual(self.server.run(third, 'rollback', first.name).returncode, 0)
        self.assertEqual(self.server.link('app'), first.name)

    def test_a_failed_rollback_leaves_the_version_in_use(self):
        first, second = self.server.release(), self.server.release()
        self.server.in_slot('a', second)
        self.server.fail('8031/healthz')
        self.assertEqual(self.server.run(second, 'rollback').returncode, 1)
        self.assertEqual((self.server.link('app'), self.server.env()['GUARDRAIL_SLOT']), (second.name, 'a'))

    def test_drill_fails_for_real_and_changes_nothing(self):
        release = self.server.release()
        self.server.in_slot('a', release)
        before = self.server.caddy_env.read_text()
        # The fake curl answers like a live service unless told otherwise : here the stopped backend must not answer.
        self.server.fail('8031/healthz')
        result = self.server.run(release, 'drill')
        self.unchanged(release, before, result)
        self.assertEqual(self.server.calls('caddy'), [])
        calls = [' '.join(c) for c in self.server.calls()]
        started = calls.index('systemctl restart guardrail-backend@b.service')
        stopped = calls.index('systemctl stop guardrail-backend@b.service', started)
        self.assertLess(stopped, next(i for i, c in enumerate(calls) if '8031/healthz' in c))

    def test_the_release_in_use_and_the_two_before_it_are_kept(self):
        releases = [self.server.release() for _ in range(5)]
        self.server.in_slot('a', releases[0])                # the oldest is still what slot a runs
        result = self.server.run(releases[4], 'deploy', str(releases[4]), DOMAIN)
        self.assertEqual(result.returncode, 0, result.stderr)
        left = sorted(p.name for p in (self.server.root / 'opt/guardrail/releases').iterdir())
        self.assertEqual(left, sorted(r.name for r in (releases[0], releases[2], releases[3], releases[4])))

    def test_status_names_the_slot_in_use_and_the_releases(self):
        first, second = self.server.release(), self.server.release()
        self.server.in_slot('b', second)
        out = self.server.run(second, 'status').stdout
        self.assertIn('지금 쓰는 칸 : b', out)
        self.assertIn(f'app → {second.name}', out)
        self.assertIn('guardrail-gateway@b.service : active', out)
        self.assertLess(out.index('  ' + second.name), out.index('  ' + first.name))


if __name__ == '__main__':
    unittest.main()
