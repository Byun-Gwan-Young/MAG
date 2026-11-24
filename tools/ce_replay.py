"""C-19-12 part 3 : replay the 2nd judgement (DM-116 ④ / DM-118) over recorded sessions, offline.

The rule's STOP steps are the recorded ones (deadlock-v0.3 + explain-v1, unchanged). At a STOP step of a dialogue task
(CS · MR · LR only; coding keeps the rule), look at the latest same-role pair of every role up to that step; if any of
them is 진전 with probability >= 0.9, the STOP is cancelled. At most 2 cancels per session; after that STOPs stand.
The session's effective first STOP is the first one not cancelled. A block-mode session ended at its STOP, so a
cancelled block STOP means "not detected" (what would have followed was never recorded).

  prepare  [--out FILE]                         session list (demo 67 from reports/c19/aggregate-main.json + the 41
                                                nc sessions in runs/demo), STOP steps, cumulative tokens, pair ids;
                                                no text. Default docs/handoff/model-track/replay/replay-input.json
  score    --input FILE --model DIR|--onnx FILE [--tokenizer DIR] --out FILE
                                                P(진전) for every pair of those sessions (needs torch/transformers,
                                                or onnxruntime + a tokenizer; run in the bge-ft venv)
  run      --input FILE [--scores FILE ...] [--trained-on main-v2.jsonl] [--out FILE]
                                                the table : rule only / human labels / each scores file

Metrics (same definitions as reports/c19/aggregate-main.json headline, DM-85) : preservation = normal-variant observe
sessions that ended in success with no effective STOP; detection = stall-variant observe sessions that stalled
(max_steps) with an effective STOP; block detection = stall-variant block sessions that stalled with a STOP not
cancelled; saving = mean share of tokens after the effective first STOP over the detected observe sessions; first STOP
step = the effective first STOP of each detected observe session.
"""
import argparse
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.ce_common import LABEL_VALUE, WORK, is_dialogue, load_jsonl, pair_id  # noqa: E402

THRESHOLD = 0.9
MAX_CANCELS = 2
CLASS = {'success': 'success', 'max_steps': 'stalled', 'escalated': 'escalated'}


# ---------------------------------------------------------------- prepare (raw session folders, no text kept)

def session_record(folder, group, meta=None):
    from tools import replay_demo
    from tools.extract_pairs import session_pairs
    rep = replay_demo.extract(folder)
    pairs, _ = session_pairs(Path(folder))
    batch = rep.get('batch') or {}
    scenario = rep['scenario_id']
    task = scenario.rsplit('-', 2)[0]
    variant = scenario.rsplit('-', 2)[1]
    rec = {'session_id': rep['session_id'], 'group': group, 'scenario_id': scenario, 'task': task,
           'variant': variant, 'mode': rep['guardrail_mode'], 'plan_id': batch.get('plan_id'),
           'termination_reason': rep['termination_reason'],
           'class': CLASS.get(rep['termination_reason'], 'other'), 'stop_steps': rep['stop_steps'],
           'cumulative_tokens': {str(s['step_id']): s['cumulative_tokens'] for s in rep['steps']},
           'total_tokens': rep['steps'][-1]['cumulative_tokens'] if rep['steps'] else 0,
           'pairs': [{'pair_id': pair_id(rep['session_id'], p['role'], p['prev_step'], p['cur_step']),
                      'role': p['role'], 'prev_step': p['prev_step'], 'cur_step': p['cur_step']} for p in pairs]}
    if meta:  # the aggregate's own values win for the demo sessions (they are the published ones)
        rec.update({'task': meta['task'], 'variant': meta['variant'], 'mode': meta['mode'], 'class': meta['class'],
                    'stop_steps': meta['stop_steps']})
        rec['aggregate_check'] = {'stop_steps_same': meta['stop_steps'] == rep['stop_steps']}
    return rec


def prepare(runs=ROOT / 'runs' / 'demo', aggregate=ROOT / 'reports' / 'c19' / 'aggregate-main.json', nc_prefix='nc-'):
    agg = json.loads(Path(aggregate).read_text(encoding='utf-8'))
    out = [session_record(runs / r['session_id'], 'demo67', r) for r in agg['session_rows']]
    skipped = []
    for folder in sorted(p for p in Path(runs).iterdir() if (p / 'conditions.json').is_file()):
        batch = json.loads((folder / 'conditions.json').read_text(encoding='utf-8')).get('batch') or {}
        if not (batch.get('plan_id') or '').startswith(nc_prefix):
            continue
        if (folder / 'session.json').is_file():
            out.append(session_record(folder, 'nc41'))
        else:  # no proxy log = no recorded STOP decisions to replay
            summary = folder / 'summary.json'
            reason = json.loads(summary.read_text(encoding='utf-8')).get('termination_reason') if summary.is_file() else None
            skipped.append({'session_id': folder.name, 'plan_id': batch.get('plan_id'), 'termination_reason': reason,
                            'why': 'no session.json (proxy log)'})
    return {'replay_input_version': 'ce-replay-input-v1', 'threshold': THRESHOLD, 'max_cancels': MAX_CANCELS,
            'sessions': out, 'skipped': skipped}


# ---------------------------------------------------------------- probabilities

def human_probs(work=WORK):
    """pair_id -> 1.0 / 0.0 from the human majority labels : main-v1 (main381 + extra177), pilot50 (4-rater
    majority), nc498 (part 0)."""
    probs = {}
    for r in load_jsonl(work / 'bge-ft' / 'main-v1.jsonl'):
        probs[r['pair_id']] = float(r['label'])
    for r in load_jsonl(work / 'nc' / 'nc498-labels-v1.jsonl'):
        probs[r['pair_id']] = float(r['label'])
    key, book = work / 'pilot50' / '파일럿50_정답키_PM전용.csv', work / '파일럿50_일치도_사람3_Claude.xlsx'
    if key.is_file() and book.is_file():
        import csv
        import openpyxl
        ids = {int(r['번호']): r['pair_id'] for r in csv.DictReader(open(key, encoding='utf-8-sig'))}
        ws = openpyxl.load_workbook(book, read_only=True, data_only=True)['입력']
        for row in ws.iter_rows(min_row=5, values_only=True):
            if isinstance(row[0], int) and row[10] in LABEL_VALUE:
                probs[ids[row[0]]] = float(LABEL_VALUE[row[10]])
    return probs


# ---------------------------------------------------------------- replay

def effective_stops(session, probs, threshold=THRESHOLD, max_cancels=MAX_CANCELS):
    """(effective first STOP or None, cancelled steps, pairs consulted without a probability)."""
    stops = sorted(session['stop_steps'])
    if not stops:
        return None, [], 0
    if not is_dialogue(session['task']) or probs is None:
        return stops[0], [], 0
    cancelled, missing = [], 0
    for s in stops:
        latest = {}
        for p in session['pairs']:
            if p['cur_step'] <= s and (p['role'] not in latest or p['cur_step'] > latest[p['role']]['cur_step']):
                latest[p['role']] = p
        values = [probs.get(p['pair_id']) for p in latest.values()]
        missing += sum(v is None for v in values)
        if len(cancelled) < max_cancels and any(v is not None and v >= threshold for v in values):
            cancelled.append(s)
            continue
        return s, cancelled, missing
    return None, cancelled, missing


def metrics(sessions, probs, trained=None):
    normal = [s for s in sessions if s['variant'] == 'normal' and s['mode'] == 'observe' and s['class'] == 'success']
    stall_obs = [s for s in sessions if s['variant'] == 'stall' and s['mode'] == 'observe' and s['class'] == 'stalled']
    stall_blk = [s for s in sessions if s['variant'] == 'stall' and s['mode'] == 'block' and s['class'] == 'stalled']
    eff = {s['session_id']: effective_stops(s, probs) for s in sessions}
    kept = sum(eff[s['session_id']][0] is None for s in normal)
    detected = [s for s in stall_obs if eff[s['session_id']][0] is not None]
    blk = sum(eff[s['session_id']][0] is not None for s in stall_blk)
    ratios = []
    for s in detected:
        first = eff[s['session_id']][0]
        total = s['total_tokens']
        ratios.append((total - s['cumulative_tokens'][str(first)]) / total if total else 0.0)
    firsts = [eff[s['session_id']][0] for s in detected]
    out = {'preservation': f'{kept}/{len(normal)}', 'preservation_rate': round(kept / len(normal), 4) if normal else None,
           'detection_observe': f'{len(detected)}/{len(stall_obs)}',
           'detection_block': f'{blk}/{len(stall_blk)}',
           'saving_mean_detected': round(statistics.mean(ratios), 4) if ratios else None,
           'first_stop_steps': sorted(firsts), 'first_stop_median': statistics.median(firsts) if firsts else None,
           'cancels': sum(len(v[1]) for v in eff.values()),
           'sessions_with_cancel': sum(bool(v[1]) for v in eff.values()),
           'pairs_without_probability': sum(v[2] for v in eff.values())}
    if trained is not None:
        out['sessions_in_training'] = f"{sum(s['session_id'] in trained for s in sessions)}/{len(sessions)}"
    return out


def run(replay_input, scores=(), trained_on=None):
    data = json.loads(Path(replay_input).read_text(encoding='utf-8'))
    sessions = data['sessions']
    trained = None
    if trained_on:
        trained = {r['session_id'] for r in load_jsonl(trained_on) if r['split'] == 'train'}
    lines = [('rule only', None), ('human labels', human_probs())]
    for path in scores:
        lines.append((Path(path).stem, json.loads(Path(path).read_text(encoding='utf-8'))['probs']))
    table = {}
    for group in ('demo67', 'nc41', 'all'):
        sel = [s for s in sessions if group == 'all' or s['group'] == group]
        table[group] = {name: metrics(sel, probs, trained if name not in ('rule only', 'human labels') else None)
                        for name, probs in lines}
    checks = {'demo_sessions': sum(s['group'] == 'demo67' for s in sessions),
              'nc_sessions': sum(s['group'] == 'nc41' for s in sessions),
              'nc_skipped': data.get('skipped', []),
              'demo_stop_steps_match_raw': all(s.get('aggregate_check', {}).get('stop_steps_same', True)
                                               for s in sessions)}
    return {'replay_version': 'ce-replay-v1', 'rule': {'threshold': THRESHOLD, 'max_cancels': MAX_CANCELS,
                                                        'tasks': 'CS · MR · LR only'},
            'checks': checks, 'table': table}


# ---------------------------------------------------------------- score (model library needed)

def score(replay_input, out, model=None, onnx=None, tokenizer=None, max_len=224, runs=ROOT / 'runs' / 'demo'):
    from tools.extract_pairs import session_pairs
    data = json.loads(Path(replay_input).read_text(encoding='utf-8'))
    rows = []
    for s in data['sessions']:
        if not is_dialogue(s['task']):
            continue
        for p in session_pairs(Path(runs) / s['session_id'])[0]:
            rows.append({'pair_id': pair_id(s['session_id'], p['role'], p['prev_step'], p['cur_step']),
                         'prev': p['prev'], 'cur': p['cur']})
    from tools.ce_train import Scorer
    scorer = Scorer(model=model, onnx=onnx, tokenizer=tokenizer, max_len=max_len)
    probs = scorer.probs(rows)
    from tools.ce_train import rel
    result = {'scores_version': 'ce-scores-v1', 'model': rel(model or onnx), 'max_len': max_len, 'pairs': len(rows),
              'probs': {r['pair_id']: round(p, 6) for r, p in zip(rows, probs)}}
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(result, indent=0), encoding='utf-8')
    return len(rows)


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('prepare')
    p.add_argument('--out', type=Path, default=WORK / 'replay' / 'replay-input.json')
    s = sub.add_parser('score')
    s.add_argument('--input', type=Path, default=WORK / 'replay' / 'replay-input.json')
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument('--model', type=Path)
    g.add_argument('--onnx', type=Path)
    s.add_argument('--tokenizer', type=Path, help='tokenizer folder for --onnx (default: the onnx file folder)')
    s.add_argument('--max_len', type=int, default=224)
    s.add_argument('--out', type=Path, required=True)
    r = sub.add_parser('run')
    r.add_argument('--input', type=Path, default=WORK / 'replay' / 'replay-input.json')
    r.add_argument('--scores', type=Path, nargs='*', default=[])
    r.add_argument('--trained-on', type=Path, default=WORK / 'bge-ft' / 'main-v2.jsonl')
    r.add_argument('--out', type=Path)
    a = ap.parse_args(argv)
    if a.cmd == 'prepare':
        data = prepare()
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(json.dumps(data, ensure_ascii=False, indent=0), encoding='utf-8')
        print(json.dumps({'sessions': len(data['sessions']),
                          'demo67': sum(x['group'] == 'demo67' for x in data['sessions']),
                          'nc41': sum(x['group'] == 'nc41' for x in data['sessions']),
                          'nc_skipped': len(data['skipped'])}))
    elif a.cmd == 'score':
        n = score(a.input, a.out, model=a.model, onnx=a.onnx, tokenizer=a.tokenizer, max_len=a.max_len)
        print(json.dumps({'pairs_scored': n, 'out': str(a.out)}))
    else:
        res = run(a.input, a.scores, a.trained_on if a.trained_on and a.trained_on.is_file() else None)
        text = json.dumps(res, ensure_ascii=False, indent=1)
        if a.out:
            a.out.parent.mkdir(parents=True, exist_ok=True)
            a.out.write_text(text + '\n', encoding='utf-8')
        print(text)
    return 0


if __name__ == '__main__':
    sys.exit(main())
