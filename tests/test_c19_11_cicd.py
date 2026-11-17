"""C-19-11 parts 1 · 2 · 2b · 3 (DM-119, DM-122) : the GitHub workflows and the scripts they and the PM run, offline.

No GitHub, no AWS, no SSH, no network : the workflows are read as YAML, and the scripts run under /bin/bash with fake
aws · curl · git on PATH and a temporary folder for HOME or /.
Checked : CI needs no secret and fails on a failed test or a network attempt ; the deploy runs only after CI on main,
reads secrets by name, takes AWS through OIDC only, and closes the runner's rule even after a failure ; the runner rule
is one address/32 on port 22 and never anything wider ; the deploy-only account's one command refuses everything that
is not a well-formed bundle ; the IAM policies allow two actions on one security group for this repository's
production environment only.
"""
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
AWS = ROOT / 'deploy' / 'aws'
WORKFLOWS = ROOT / '.github' / 'workflows'
sys.path.insert(0, str(ROOT / 'tools'))
import ci_check  # noqa: E402

SG = 'sg-0123456789abcdef0'
ACCOUNT = '123456789012'
SECRETS = {'DEPLOY_SSH_KEY', 'DEPLOY_KNOWN_HOSTS', 'DEPLOY_HOST', 'AWS_SECURITY_GROUP_ID', 'AWS_DEPLOY_ROLE_ARN'}

FAKE_AWS = textwrap.dedent('''\
    #!{python}
    import json, os, sys
    args = sys.argv[1:]
    with open(os.environ['FAKE_LOG'], 'a') as log:
        log.write(json.dumps(args) + '\\n')
    error = os.environ.get('FAKE_AWS_ERROR')
    if error and any(a.endswith('-security-group-ingress') for a in args):
        sys.stderr.write(f'An error occurred ({{error}}) when calling the operation: rule sgr-0abc {sg}\\n')
        sys.exit(254)
    if 'get-caller-identity' in args:
        print('{account}')
    elif 'describe-security-groups' in args:
        print('{sg}' if '--group-ids' not in args else '198.51.100.7/32')
    else:
        print(json.dumps({{'Return': True, 'SecurityGroupRules': [{{'SecurityGroupRuleId': 'sgr-0abc', 'GroupId': '{sg}'}}]}}))
''')


def load(name):
    data = yaml.safe_load((WORKFLOWS / name).read_text(encoding='utf-8'))
    data['on'] = data.pop(True) if True in data else data['on']     # YAML reads the key `on` as a boolean
    return data


def runs(job):
    return '\n'.join(step.get('run', '') for step in job['steps'])


class Workflows(unittest.TestCase):
    def setUp(self):
        self.ci, self.deploy = load('ci.yml'), load('deploy.yml')
        self.ci_text = (WORKFLOWS / 'ci.yml').read_text(encoding='utf-8')
        self.deploy_text = (WORKFLOWS / 'deploy.yml').read_text(encoding='utf-8')

    def test_ci_runs_on_pull_requests_and_when_the_deploy_calls_it_without_any_secret(self):
        self.assertEqual(set(self.ci['on']), {'pull_request', 'workflow_call'})
        self.assertEqual(self.ci['permissions'], {'contents': 'read'})
        self.assertNotRegex(self.ci_text, r'secrets\.|id-token|\bvars\.')
        python = runs(self.ci['jobs']['python'])
        self.assertIn('tools/run_offline_tests.py --baseline tests/fixtures/p0-3-mechanism-test-ids.json', python)
        self.assertLess(python.index('tools/run_offline_tests.py'), python.index('python tools/ci_check.py'))
        self.assertIn('python tools/final_freeze.py check', python)
        self.assertEqual([s['run'] for s in self.ci['jobs']['frontend']['steps'] if 'run' in s],
                         ['npm ci', 'npm run build', 'npm test'])
        self.assertNotIn('playwright', self.ci_text.replace('(Playwright)', ''))   # no browser download in CI
        for job in self.ci['jobs'].values():
            self.assertIn('timeout-minutes', job)

    def test_deploy_runs_only_after_ci_passed_on_main(self):
        self.assertEqual(self.deploy['on'], {'push': {'branches': ['main']}, 'workflow_dispatch': None})
        jobs = self.deploy['jobs']
        self.assertEqual(jobs['ci']['uses'], './.github/workflows/ci.yml')
        self.assertEqual(jobs['deploy']['needs'], 'ci')
        for job in jobs.values():
            self.assertEqual(job['if'], "github.ref == 'refs/heads/main'")
        self.assertEqual(self.deploy['concurrency'], {'group': 'deploy-production', 'cancel-in-progress': False})
        self.assertEqual(jobs['deploy']['environment'], 'production')

    def test_only_the_deploy_job_may_ask_for_an_oidc_token(self):
        self.assertEqual(self.deploy['permissions'], {'contents': 'read'})
        self.assertEqual(self.deploy['jobs']['ci']['permissions'], {'contents': 'read'})
        self.assertEqual(self.deploy['jobs']['deploy']['permissions'], {'contents': 'read', 'id-token': 'write'})

    def test_secrets_are_read_by_name_and_no_long_lived_aws_key_exists(self):
        self.assertEqual(set(re.findall(r'secrets\.([A-Z_]+)', self.deploy_text)), SECRETS)
        self.assertEqual(set(re.findall(r'vars\.([A-Z_]+)', self.deploy_text)), {'AWS_REGION'})
        aws = next(s for s in self.deploy['jobs']['deploy']['steps'] if 'configure-aws-credentials' in s.get('uses', ''))
        self.assertEqual(set(aws['with']), {'role-to-assume', 'aws-region', 'role-session-name', 'role-duration-seconds'})
        for text in (self.ci_text, self.deploy_text):
            code = '\n'.join(l.split('#', 1)[0] for l in text.splitlines())
            self.assertNotRegex(code, r'(?i)aws_access_key_id|aws_secret_access_key|AKIA[0-9A-Z]{16}')
            self.assertNotRegex(code, r'set -[a-z]*x|\bssh -v|ACTIONS_STEP_DEBUG')
            self.assertNotRegex(code, r'\b\d{1,3}(?:\.\d{1,3}){3}\b')           # no address
            self.assertNotRegex(code, r'echo .*\$\{\{ *secrets')              # a secret is never echoed
            self.assertNotIn('pull_request_target', code)
        # A secret reaches a script only through the step's env, never inside the command line.
        for step in self.deploy['jobs']['deploy']['steps']:
            self.assertNotIn('secrets.', step.get('run', ''))

    def test_the_runner_rule_is_closed_even_when_the_deploy_failed(self):
        steps = self.deploy['jobs']['deploy']['steps']
        index = {s.get('run', '').strip(): i for i, s in enumerate(steps)}
        opened = index['bash deploy/aws/ci-runner-access.sh open']
        sent = index['bash deploy/aws/ci-send.sh "$RUNNER_TEMP/bundle/bundle.tar.gz"']
        closed = index['bash deploy/aws/ci-runner-access.sh close']
        self.assertLess(opened, sent)
        self.assertLess(sent, closed)
        self.assertEqual(steps[closed]['if'], 'always()')
        self.assertEqual(closed, len(steps) - 1)
        self.assertFalse(any('continue-on-error' in s for s in steps))         # a failed switch fails the job
        bundle = next(s['run'] for s in steps if 'bundle.py' in s.get('run', ''))
        self.assertIn('deploy/aws/bundle.py build --ref HEAD', bundle)
        self.assertLess(next(i for i, s in enumerate(steps) if 'bundle.py' in s.get('run', '')), opened)

    def test_actions_are_the_official_ones_at_a_fixed_major(self):
        used = set(re.findall(r'uses: (\S+)', self.ci_text + self.deploy_text))
        self.assertEqual(used, {'actions/checkout@v4', 'actions/setup-python@v5', 'actions/setup-node@v4',
                                'actions/upload-artifact@v4', 'aws-actions/configure-aws-credentials@v4',
                                './.github/workflows/ci.yml'})
        for text in (self.ci_text, self.deploy_text):
            self.assertEqual(text.count('persist-credentials: false'), text.count('actions/checkout@v4'))


class CiCheck(unittest.TestCase):
    GOOD = {'total': {'executed': 10, 'passed': 9, 'failures': 0, 'errors': 0, 'skipped': 1},
            'network_attempts': [], 'timed_out_modules': [], 'missing_baseline_ids': []}

    def check(self, result):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'tests.json'
            if result is not None:
                path.write_text(json.dumps(result), encoding='utf-8')
            out = io.StringIO()
            stdout, sys.stdout = sys.stdout, out
            try:
                return ci_check.main([str(path)]), out.getvalue()
            finally:
                sys.stdout = stdout

    def test_pass_needs_no_failure_and_no_network_attempt(self):
        self.assertEqual(self.check(self.GOOD)[0], 0)
        for change in ({'network_attempts': ['socket.connect']}, {'timed_out_modules': ['m']},
                       {'missing_baseline_ids': ['t']}, {'total': {**self.GOOD['total'], 'failures': 1}},
                       {'total': {**self.GOOD['total'], 'errors': 1}}, {'total': {**self.GOOD['total'], 'executed': 0}}):
            self.assertEqual(self.check({**self.GOOD, **change})[0], 1, change)
        without = {k: v for k, v in self.GOOD.items() if k != 'network_attempts'}
        self.assertEqual(self.check(without)[0], 1)              # an unknown is not a pass
        self.assertEqual(self.check(None)[0], 1)                 # the run did not finish

    def test_a_failure_names_the_test_but_prints_no_test_output(self):
        result = {**self.GOOD, 'total': {**self.GOOD['total'], 'failures': 1},
                  'failures': [{'test': 'tests.test_x.T.test_y', 'traceback': 'value-that-must-not-print'}]}
        code, out = self.check(result)
        self.assertEqual(code, 1)
        self.assertIn('tests.test_x.T.test_y', out)
        self.assertNotIn('value-that-must-not-print', out)


class Shell(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.bin = self.tmp / 'bin'
        self.bin.mkdir()
        (self.bin / 'aws').write_text(FAKE_AWS.format(python=sys.executable, account=ACCOUNT, sg=SG), encoding='utf-8')
        self.answer('198.51.100.7')
        (self.bin / 'aws').chmod(0o755)
        self.log = self.tmp / 'calls.log'
        self.env = {'PATH': f'{self.bin}:/usr/bin:/bin', 'HOME': str(self.tmp / 'home'), 'LANG': 'C.UTF-8',
                    'FAKE_LOG': str(self.log), 'RUNNER_TEMP': str(self.tmp)}
        (self.tmp / 'home').mkdir()

    def answer(self, text):
        (self.bin / 'curl').write_text(f"#!/bin/sh\nprintf '%s\\n' '{text}'\n", encoding='utf-8')
        (self.bin / 'curl').chmod(0o755)

    def run_script(self, name, *args, stdin=None, **env):
        return subprocess.run(['/bin/bash', str(AWS / name), *args], input=stdin, capture_output=True, timeout=60,
                              env={**self.env, **env})

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []


class RunnerAccess(Shell):
    ENV = {'AWS_SECURITY_GROUP_ID': SG, 'AWS_REGION': 'ap-northeast-2'}

    def access(self, verb, **env):
        result = self.run_script('ci-runner-access.sh', verb, **{**self.ENV, **env})
        return result.returncode, (result.stdout + result.stderr).decode()

    def test_open_adds_one_address_on_port_22_and_close_removes_the_same(self):
        code, out = self.access('open')
        self.assertEqual(code, 0, out)
        self.assertLess(out.index('::add-mask::198.51.100.7'), out.index('port 22 opened'))
        code, closed = self.access('close')
        self.assertEqual(code, 0, closed)
        rule = ['--region', 'ap-northeast-2', '--group-id', SG, '--protocol', 'tcp', '--port', '22',
                '--cidr', '198.51.100.7/32']
        self.assertEqual(self.calls(), [['ec2', 'authorize-security-group-ingress', *rule],
                                        ['ec2', 'revoke-security-group-ingress', *rule]])
        for text in (out, closed):                                   # nothing of AWS's answer, not the group
            self.assertNotIn(SG, text)
            self.assertNotIn('sgr-', text)
        self.assertFalse((self.tmp / 'guardrail-runner-ip').exists())
        self.assertEqual(self.access('close')[0], 0)                 # a second close : nothing to do, no call
        self.assertEqual(len(self.calls()), 2)

    def test_the_rule_is_never_wider_than_one_address(self):
        for answer in ('0.0.0.0', '198.51.100.7/0', '198.51.100.0/24', '999.1.1.1', '198.51.100.7 0.0.0.0/0', '', '::/0'):
            with self.subTest(answer=answer):
                self.answer(answer)
                self.assertNotEqual(self.access('open')[0], 0)
        self.assertEqual(self.calls(), [])
        script = (AWS / 'ci-runner-access.sh').read_text(encoding='utf-8')
        code = '\n'.join(l.split('#', 1)[0] for l in script.splitlines())
        self.assertEqual(re.findall(r'--cidr (\S+)', code), ['"$2/32"'])
        self.assertEqual(re.findall(r'--port (\S+)', code), ['22'])
        self.assertNotRegex(code, r'set -[a-z]*x')

    def test_close_still_runs_after_an_open_that_aws_refused(self):
        code, out = self.access('open', FAKE_AWS_ERROR='UnauthorizedOperation')
        self.assertNotEqual(code, 0)
        self.assertIn('UnauthorizedOperation', out)
        self.assertNotIn(SG, out)
        self.assertEqual(self.access('close', FAKE_AWS_ERROR='InvalidPermission.NotFound')[0], 0)
        self.assertEqual([c[1] for c in self.calls()], ['authorize-security-group-ingress',
                                                        'revoke-security-group-ingress'])

    def test_a_rule_left_by_an_earlier_run_is_fine_and_a_failed_close_is_loud(self):
        self.assertEqual(self.access('open', FAKE_AWS_ERROR='InvalidPermission.Duplicate')[0], 0)
        code, out = self.access('close', FAKE_AWS_ERROR='RequestLimitExceeded')
        self.assertNotEqual(code, 0)
        self.assertIn('NOT REMOVED', out)
        self.assertTrue((self.tmp / 'guardrail-runner-ip').exists())

    def test_group_and_region_must_look_right(self):
        for env in ({'AWS_SECURITY_GROUP_ID': ''}, {'AWS_SECURITY_GROUP_ID': 'sg-1; rm -rf /'}, {'AWS_REGION': ''},
                    {'AWS_REGION': 'x y'}):
            self.assertNotEqual(self.access('open', **env)[0], 0, env)
        self.assertEqual(self.calls(), [])


class Send(Shell):
    def test_nothing_is_sent_without_the_three_values_and_a_bundle(self):
        bundle = self.tmp / 'bundle.tar.gz'
        bundle.write_bytes(b'x')
        good = {'DEPLOY_SSH_KEY': 'k', 'DEPLOY_KNOWN_HOSTS': 'h', 'DEPLOY_HOST': 'server.example.org'}
        (self.bin / 'ssh').write_text('#!/bin/sh\nexit 97\n', encoding='utf-8')
        (self.bin / 'ssh').chmod(0o755)
        for missing in good:
            result = self.run_script('ci-send.sh', str(bundle), **{k: v for k, v in good.items() if k != missing})
            self.assertEqual(result.returncode, 1, missing)
        for host in ('a b', '-oProxyCommand=x', 'x;y', 'user@host'):
            self.assertEqual(self.run_script('ci-send.sh', str(bundle), **{**good, 'DEPLOY_HOST': host}).returncode, 1)
        self.assertEqual(self.run_script('ci-send.sh', str(self.tmp / 'none'), **good).returncode, 1)

    def test_the_host_key_is_pinned_and_the_key_file_does_not_outlive_the_script(self):
        text = (AWS / 'ci-send.sh').read_text(encoding='utf-8')
        for option in ('StrictHostKeyChecking=yes', 'UserKnownHostsFile="$work/known_hosts"', 'IdentitiesOnly=yes',
                       'BatchMode=yes', 'GlobalKnownHostsFile=/dev/null', '"$DEPLOY_USER@$DEPLOY_HOST" < "$BUNDLE"'):
            self.assertIn(option, text)
        self.assertIn("trap 'rm -rf \"$work\"' EXIT", text)
        self.assertIn('DEPLOY_USER=guardrail-deploy', text)
        code = '\n'.join(l.split('#', 1)[0] for l in text.splitlines())
        self.assertNotRegex(code, r'set -[a-z]*x|ssh -v|accept-new|StrictHostKeyChecking=no')
        # The two secrets are written to their owner-only files and nowhere else.
        self.assertEqual(re.findall(r'(?:echo|printf)[^\n]*\$\{?DEPLOY_(?:SSH_KEY|KNOWN_HOSTS|HOST)\b[^\n]*', code),
                         ['printf \'%s\\n\' "$DEPLOY_SSH_KEY" > "$work/key"',
                          'printf \'%s\\n\' "$DEPLOY_KNOWN_HOSTS" > "$work/known_hosts" )'])


def bundle_bytes(members):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as tar:
        for name, body in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    return buffer.getvalue()


INSTALLER = b'#!/bin/bash\nprintf "%s\\n" "$#" "$2" "[$3]" > "$GUARDRAIL_PREFIX/installed"\ntar -tzf "$1" >/dev/null\n'
GOOD = {'DEPLOY_INFO.json': b'{}', 'deploy/aws/remote-install.sh': INSTALLER, 'deploy/aws/slots.sh': b'', 'service/api.py': b''}


class DeployCommand(Shell):
    """deploy/aws/guardrail-deploy.sh : the deploy-only account's one command."""

    def setUp(self):
        super().setUp()
        self.root = self.tmp / 'root'
        for folder in ('var/tmp', 'etc/guardrail', 'run/lock'):
            (self.root / folder).mkdir(parents=True)
        (self.root / 'etc/guardrail/caddy.env').write_text('GUARDRAIL_DOMAIN=demo.example.org\n')

    def deploy(self, stdin, *args):
        result = self.run_script('guardrail-deploy.sh', *args, stdin=stdin, GUARDRAIL_PREFIX=str(self.root))
        return result.returncode, result.stderr.decode()

    def test_a_bundle_on_stdin_is_installed_with_the_servers_own_domain(self):
        code, err = self.deploy(bundle_bytes(GOOD))
        self.assertEqual(code, 0, err)
        self.assertEqual((self.root / 'installed').read_text().split('\n')[:3], ['3', 'demo.example.org', '[]'])
        self.assertEqual(list((self.root / 'var/tmp').iterdir()), [])          # nothing left behind

    def test_everything_else_is_refused_before_anything_runs(self):
        without = lambda name: {k: v for k, v in GOOD.items() if k != name}
        cases = {'empty': b'', 'not a tar': b'#!/bin/sh\nid\n' * 50, 'escapes': bundle_bytes({**GOOD, '../evil': b'x'}),
                 'absolute': bundle_bytes({**GOOD, '/etc/cron.d/evil': b'x'}),
                 'inner escape': bundle_bytes({**GOOD, 'a/../../evil': b'x'}),
                 'no installer': bundle_bytes(without('deploy/aws/remote-install.sh')),
                 'no info': bundle_bytes(without('DEPLOY_INFO.json'))}
        for name, stdin in cases.items():
            with self.subTest(name=name):
                self.assertEqual(self.deploy(stdin)[0], 1)
        self.assertEqual(self.deploy(bundle_bytes(GOOD), 'anything')[0], 1)     # no argument is taken
        (self.root / 'etc/guardrail/caddy.env').write_text('GUARDRAIL_DOMAIN=bad domain\n')
        self.assertEqual(self.deploy(bundle_bytes(GOOD))[0], 1)
        self.assertFalse((self.root / 'installed').exists())

    def test_a_failed_install_is_the_commands_failure(self):
        failing = {**GOOD, 'deploy/aws/remote-install.sh': b'#!/bin/bash\nexit 1\n'}
        self.assertEqual(self.deploy(bundle_bytes(failing))[0], 1)

    def test_the_account_is_tied_to_that_one_command(self):
        setup = (AWS / 'deploy-user-setup.sh').read_text(encoding='utf-8')
        self.assertIn('''printf 'restrict,command="sudo -n %s" %s\\n' "$COMMAND" "$key"''', setup)
        self.assertIn('''printf '%s ALL=(root) NOPASSWD: %s ""\\n' "$USER_NAME" "$COMMAND"''', setup)   # "" : no argument
        self.assertIn('COMMAND=/usr/local/sbin/guardrail-deploy', setup)
        self.assertIn('install -o root -g root -m 755 "$SCRIPT" "$COMMAND"', setup)
        self.assertLess(setup.index('visudo -cf "$tmp"'), setup.index('mv "$tmp" /etc/sudoers.d/guardrail-deploy'))
        code = '\n'.join(l.split('#', 1)[0] for l in setup.splitlines())
        self.assertNotRegex(code, r'usermod|adduser .* sudo|-G\b|NOPASSWD: ALL|>> ')   # no group, one key, one rule
        key_rule = re.search(r'\[\[ "\$key" =~ (.+?) \]\] \|\| die "ed25519', setup).group(1)
        def ok(line):
            return subprocess.run(['/bin/bash', '-c', f'[[ "$1" =~ {key_rule} ]]', '_', line]).returncode == 0
        body = 'AAAAC3NzaC1lZDI1NTE5AAAAI' + 'B' * 43
        self.assertTrue(ok(f'ssh-ed25519 {body} guardrail-deploy-ci'))
        self.assertTrue(ok(f'ssh-ed25519 {body}'))
        for bad in (f'command="sh" ssh-ed25519 {body}', f'ssh-rsa {body}', f'ssh-ed25519 {body} a b',
                    f'ssh-ed25519 {body} x\nssh-ed25519 {body}', '-----BEGIN OPENSSH PRIVATE KEY-----'):
            self.assertFalse(ok(bad), bad)


class PmSetup(Shell):
    def setUp(self):
        super().setUp()
        (self.bin / 'git').write_text("#!/bin/sh\necho 'https://github.com/example-owner/example-repo.git'\n")
        (self.bin / 'git').chmod(0o755)

    def out(self, *args):
        result = self.run_script('ci-setup.sh', *args)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        return result.stdout.decode()

    def test_trust_policy_is_this_repositorys_production_environment_only(self):
        policy = json.loads(self.out('trust-policy'))
        (statement,) = policy['Statement']
        host = 'token.actions.githubusercontent.com'
        self.assertEqual(statement['Principal'], {'Federated': f'arn:aws:iam::{ACCOUNT}:oidc-provider/{host}'})
        self.assertEqual(statement['Action'], 'sts:AssumeRoleWithWebIdentity')
        self.assertEqual(statement['Condition'], {'StringEquals': {
            f'{host}:aud': 'sts.amazonaws.com',
            f'{host}:sub': 'repo:example-owner/example-repo:environment:production'}})      # no wildcard

    def test_permission_policy_is_two_actions_on_one_security_group(self):
        policy = json.loads(self.out('permission-policy'))
        (statement,) = policy['Statement']
        self.assertEqual(statement, {'Effect': 'Allow', 'Action': ['ec2:AuthorizeSecurityGroupIngress',
                                                                   'ec2:RevokeSecurityGroupIngress'],
                                     'Resource': f'arn:aws:ec2:ap-northeast-2:{ACCOUNT}:security-group/{SG}'})
        self.assertNotIn('*', json.dumps(policy))
        self.assertFalse(any(v for c in self.calls() for v in c if v.startswith(('create-', 'put-', 'attach-', 'authorize-'))))

    def test_names_lists_exactly_what_the_workflow_reads(self):
        names = self.out('names')
        listed = set(re.findall(r'^  ([A-Z_]+) ', names, re.M))
        self.assertEqual(listed, SECRETS | {'AWS_REGION'})
        self.assertEqual(self.out('value', 'AWS_REGION').strip(), 'ap-northeast-2')
        self.assertEqual(self.out('value', 'AWS_SECURITY_GROUP_ID').strip(), SG)
        self.assertEqual(self.out('ssh-rules').strip(), '198.51.100.7/32')

    def test_the_private_key_is_made_once_and_goes_to_a_pipe_only(self):
        self.assertIn('guardrail-deploy-ci', self.out('keygen'))
        key = self.tmp / 'home/.ssh/guardrail-deploy-ci'
        self.assertEqual(key.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.out('value', 'DEPLOY_SSH_KEY'), key.read_text())
        self.assertNotEqual(self.run_script('ci-setup.sh', 'keygen').returncode, 0)   # never overwritten
        text = (AWS / 'ci-setup.sh').read_text(encoding='utf-8')
        self.assertIn('[[ ! -t 1 ]] || die "개인 키는 화면에 찍지 않는다', text)
        self.assertNotIn('guardrail-demo.pem"', text)                                # the administrator key is not read
        self.assertNotEqual(self.run_script('ci-setup.sh', 'value', 'OTHER').returncode, 0)


if __name__ == '__main__':
    unittest.main()
