"""C-19-7 (DM-91 ⑤) : the server bundle — only committed files of one git ref, checked before anything leaves the Mac.

  venv/bin/python -B deploy/aws/bundle.py build --out DIR [--ref main]

1. `git archive <ref>` of the INCLUDE paths minus the EXCLUDE paths (both lists below) : the query API (service/), the
   storage layer (storage/), the few modules they import, and the small committed demo records the loader and the API
   read. The proxy, the runner, the batch runner, keys, raw session logs, docs/handoff and venv never go.
2. The screen (C-19-10, DM-111 · DM-114) : the React build `frontend/dist` (made on the PM's computer with `npm ci` and
   `npm run build` ; no Node on the server) goes in as site/ — missing dist stops with "npm run build first", and a dist
   older than the frontend sources, or frontend sources that differ from <ref>, is a problem. From a full
   `git archive <ref>` in a temporary folder (both builds read the proxy code and configuration for the system page ;
   those files stay on the Mac) : the frozen reference data the React screen reads at /api/frontend/reference
   (`frontend/export_reference.py`) as site/reference.json, and the old Python demo screen (`tools/demo_ui.py build
   --api`, kept for the presentation) as site/legacy/index.html.
3. Checks on every bundled file (a failure stops with no bundle) : no path matches EXCLUDE, no file over
   MAX_FILE_BYTES, no key pattern (also in the built JS · CSS) (D-11 patterns and the key values set in this shell, service.store.key_hits), no model
   text field (narrative, reasoning text, message content) in any JSON file.
4. Writes DIR/bundle.tar.gz, DIR/bundle-files.txt (path and bytes per line) and DIR/remote-install.sh (the committed
   copy), and prints a JSON summary. Offline : git and the local Python only.
"""
import argparse
from datetime import datetime, timezone
import fnmatch
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from service.store import forbidden_fields, key_hits  # noqa: E402

BUNDLE_VERSION = 'guardrail-server-bundle-v2'   # v2 (C-19-10) : site/ is the React build
DIST = ROOT / 'frontend' / 'dist'
# What `npm run build` reads (the rest of frontend/ is local tooling and tests).
FRONTEND_SOURCES = ('frontend/src', 'frontend/public', 'frontend/index.html', 'frontend/package.json',
                    'frontend/package-lock.json', 'frontend/vite.config.js')
# Files the bundle itself puts under site/ ; the React build must not bring its own.
SITE_RESERVED = ('site/reference.json', 'site/legacy/')
# What the server needs : the API and loader code, the modules they import (service.store -> tools.analyze_session ->
# experiments protocol constants, scenario_schema, json_subset, progress_schema, tools.gateway_prices), the deploy files,
# and the committed demo records the loader (storage demo-all, service.load_demo check) and the API read.
INCLUDE = (
    'service/',
    'storage/',
    'backend/',                   # DM-108 : service backend (Google sign-in, account, projects) ; its tests stay out
    'deploy/aws/',
    'tools/analyze_session.py',
    'tools/gateway_prices.py',
    'experiments/step_execution.py',
    'experiments/issue_contract.py',
    'experiments/output_contract.py',
    'scenario_schema.py',
    'json_subset.py',
    'progress_schema.py',
    'reports/c19/aggregate-main.json',
    'reports/c18-8/exclusions.json',
    'reports/c18-8/account-corrections.json',
    'docs/criteria/demo-final-freeze.json',
    'scenarios/README.md',        # Korean scenario names (service/load_demo.scenario_rows) ; the YAMLs stay
    'batch_plans/',
    'runs/demo/*/summary.json',
    'runs/demo/*/scenario.yaml',
    'runs/demo/*/conditions.json',
    'runs/demo/*/record/*.json',
)
# Never on the server (DM-91 ⑤), even if an INCLUDE path would reach it. Checked again on the bundled file list.
EXCLUDE = (
    'logs/*',                     # proxy session logs (raw)
    'backend/test_*.py',          # backend tests (run on the Mac, not on the server)
    'docs/handoff/*',             # PM hand-over documents
    'venv/*', '.venv/*',          # local virtual environments
    '.env', '.env.*', '*/.env', '*/.env.*',   # key files
    '*.pem', '*.key', '*id_rsa*', '*id_ed25519*',
    'deploy/aws/target.env',      # deploy target values (git-ignored anyway)
    'runs/*/session.json',        # raw session files (model text) and other large per-step files ('*' crosses '/')
    'runs/*/steps.json',
    'runs/*/observations/*',
    'runs/*/workspace/*',
    'runs/*/search/*',
    'runs/*/tool-dispatch.json',
    'runs/*/prompt-trend.json',
    'proxy.py', 'experiments/run_scenario.py', 'experiments/dialogue_runner.py', 'tools/run_batch.py',
)
MAX_FILE_BYTES = 1024 * 1024
TEXT_SUFFIXES = {'.py', '.json', '.yaml', '.yml', '.md', '.txt', '.sql', '.sh', '.service', '.template', '.example',
                 '.html', '.toml', '.cfg', '.js', '.css', '.svg', ''}


def git(*args, **kwargs):
    return subprocess.run(['git', '-C', str(ROOT), *args], check=True, capture_output=True, **kwargs)


def excluded(path):
    return any(fnmatch.fnmatch(path, pattern) for pattern in EXCLUDE)


def pathspecs():
    # git's default pathspec wildcard is fnmatch without FNM_PATHNAME ('*' also matches '/'), the same as excluded().
    return list(INCLUDE) + [f':(exclude){p}' for p in EXCLUDE]


def archive_members(ref, specs=None):
    """{path: bytes} of the committed files of `ref` (all of them, or those the pathspecs pick)."""
    data = git('archive', '--format=tar', ref, '--', *(specs or [])).stdout
    out = {}
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        for member in tar.getmembers():
            if member.isfile():
                out[member.name] = tar.extractfile(member).read()
    return out


def build_from_archive(ref, workdir):
    """From a full archive of `ref` in a temporary folder : (reference JSON bytes, legacy screen bytes, screen info)."""
    full = Path(workdir) / 'full'
    full.mkdir()
    data = git('archive', '--format=tar', ref).stdout
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        tar.extractall(full, filter='data')
    # The same export the local adapter serves (frontend/dev_server.py) ; it writes frontend/public/records.json.
    result = subprocess.run([sys.executable, '-B', str(full / 'frontend' / 'export_reference.py')],
                            capture_output=True, text=True, cwd=full)
    if result.returncode != 0:
        raise SystemExit(f'frontend reference export failed: {result.stdout[-500:]} {result.stderr[-500:]}')
    reference = (full / 'frontend' / 'public' / 'records.json').read_bytes()
    output = Path(workdir) / 'index.html'
    result = subprocess.run([sys.executable, '-B', str(full / 'tools' / 'demo_ui.py'), 'build', '--api',
                             '--aggregate', str(full / 'reports' / 'c19' / 'aggregate-main.json'),
                             '--output', str(output), '--runs-root', str(full / 'runs' / 'demo')],
                            capture_output=True, text=True, cwd=full)
    if result.returncode != 0:
        raise SystemExit(f'demo screen build failed: {result.stdout[-500:]} {result.stderr[-500:]}')
    return reference, output.read_bytes(), json.loads(result.stdout)


def source_problems(commit, built_at):
    """Why frontend/dist may not be the build of `commit` : sources that differ from it, or a newer source file."""
    problems = []
    diff = subprocess.run(['git', '-C', str(ROOT), 'diff', '--name-only', commit, '--', *FRONTEND_SOURCES],
                          capture_output=True, text=True, check=True).stdout.split()
    untracked = git('ls-files', '--others', '--exclude-standard', '--', *FRONTEND_SOURCES, text=True).stdout.split()
    for path in sorted(set(diff + untracked)):
        problems.append(f'frontend source differs from {commit[:12]}: {path}')
    tracked = git('ls-files', '--', *FRONTEND_SOURCES, text=True).stdout.split()
    newer = [p for p in tracked if (ROOT / p).is_file() and (ROOT / p).stat().st_mtime > built_at]
    if newer:
        problems.append(f'frontend/dist is older than {newer[0]} (and {len(newer) - 1} more) : npm run build again')
    return problems


def frontend_build(commit, dist=DIST, check_sources=True):
    """({site/... : bytes} of the React build, problems). No build at all stops : the PM runs npm run build first."""
    dist = Path(dist)
    index = dist / 'index.html'
    if not index.is_file():
        raise SystemExit(f'{dist} has no React build (index.html) : run `npm ci` and `npm run build` in frontend first')
    files = {f'site/{p.relative_to(dist).as_posix()}': p.read_bytes() for p in sorted(dist.rglob('*')) if p.is_file()}
    problems = [f'React build brings a reserved path: {p}' for p in files if p.startswith(SITE_RESERVED)]
    if check_sources:
        problems += source_problems(commit, index.stat().st_mtime)
    return files, problems


def check(files):
    """Problems in the bundle file map {path: bytes} (empty when it may leave the Mac)."""
    problems = []
    for path, body in sorted(files.items()):
        if excluded(path):
            problems.append(f'excluded path: {path}')
        if len(body) > MAX_FILE_BYTES:
            problems.append(f'file over {MAX_FILE_BYTES} bytes: {path} ({len(body)})')
        if Path(path).suffix in TEXT_SUFFIXES:
            text = body.decode('utf-8', errors='replace')
            for hit in key_hits(text):
                problems.append(f'key pattern {hit}: {path}')
            if path.endswith('.json'):
                try:
                    found = forbidden_fields(json.loads(text))
                except ValueError:
                    found = []
                if found:
                    problems.append(f'model text field {found[0]}: {path}')
    return problems


def build(ref='main', out=None, dist=DIST, check_sources=True):
    commit = git('rev-parse', '--verify', f'{ref}^{{commit}}', text=True).stdout.strip()
    site, site_problems = frontend_build(commit, dist, check_sources)
    files = archive_members(commit, pathspecs())
    with tempfile.TemporaryDirectory() as workdir:
        reference, legacy, screen_info = build_from_archive(commit, workdir)
    files.update(site)
    files['site/reference.json'] = reference
    files['site/legacy/index.html'] = legacy
    site_hash = hashlib.sha256()
    for path, body in sorted(site.items()):
        site_hash.update(f'{path}\t{len(body)}\n'.encode('utf-8'))
        site_hash.update(body)
    info = {'bundle_version': BUNDLE_VERSION, 'ref': ref, 'commit': commit,
            'built_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'frontend': {'files': len(site), 'bytes': sum(len(b) for b in site.values()),
                         'sha256': site_hash.hexdigest(),
                         'reference_sessions': len(json.loads(reference)['sessions'])},
            'screen': {'sessions': screen_info['sessions'], 'api': screen_info['api'],
                       'replay_missing': screen_info['replay_missing'], 'path': '/legacy/'},
            'files': len(files) + 1, 'bytes': sum(len(b) for b in files.values())}
    files['DEPLOY_INFO.json'] = (json.dumps(info, ensure_ascii=False, indent=1) + '\n').encode('utf-8')
    problems = (site_problems + check(files)
                + (['screen sessions missing replay.json'] if screen_info['replay_missing'] else []))
    summary = {**info, 'bytes': sum(len(b) for b in files.values()), 'problems': problems}
    if problems or out is None:
        return summary, files
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as tar:
        for path, body in sorted(files.items()):
            member = tarfile.TarInfo(path)
            member.size, member.mode, member.mtime = len(body), 0o644, 0
            tar.addfile(member, io.BytesIO(body))
    (out / 'bundle.tar.gz').write_bytes(buffer.getvalue())
    (out / 'bundle-files.txt').write_text(''.join(f'{p}\t{len(b)}\n' for p, b in sorted(files.items())),
                                          encoding='utf-8')
    (out / 'remote-install.sh').write_bytes(files['deploy/aws/remote-install.sh'])
    summary.update({'bundle': str(out / 'bundle.tar.gz'), 'bundle_bytes': len(buffer.getvalue()),
                    'bundle_sha256': hashlib.sha256(buffer.getvalue()).hexdigest()})
    return summary, files


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest='command', required=True)
    one = sub.add_parser('build')
    one.add_argument('--ref', default='main')
    one.add_argument('--out', type=Path, required=True)
    one.add_argument('--dist', type=Path, default=DIST, help='the React build (default frontend/dist)')
    args = parser.parse_args(argv)
    summary, _ = build(args.ref, args.out, args.dist)
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 1 if summary['problems'] else 0


if __name__ == '__main__':
    sys.exit(main())
