"""C-19-12 part 1 : data v2 = main-v1 (557 : train 477 + test 80) + the 498 non-coding pairs (DM-123).

  venv/bin/python -B tools/ce_data_v2.py            write bge-ft/main-v2.jsonl and print the split (counts only)
  venv/bin/python -B tools/ce_data_v2.py --dry-run  print the split, write nothing

- The 80 old test pairs stay split 'test' (comparison with the earlier results), the old train rows keep their folds.
- The nc pairs are split BY SESSION : about 20 % of the sessions of every (task, variant) stratum -- at least one
  where the stratum has two or more sessions -- become the new test 'test-nc'; the rest join 'train' and get a
  fold_train 0..4 (also by session, stratum by stratum). Fixed seed. No session has pairs in two splits.
- Labels come from nc/nc498-labels-v1.jsonl (part 0 majority). Rows carry the same fields as main-v1, plus source 'nc498'.
Prints sessions / pairs per split, task and variant, and the test-nc session ids; never utterance text.
"""
import argparse
import json
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.ce_common import DOMAIN, WORK, load_jsonl, pair_id, write_jsonl  # noqa: E402

SEED = 20261125
TEST_SHARE = 0.2
FOLDS = 5
SESSION_CLASS = {'success': 'success', 'max_steps': 'stalled', 'escalated': 'escalated'}


def scenario_of(session_id):
    return session_id.rsplit('-', 1)[0]


def nc_rows(pairs, labels):
    by_no = {r['no']: r for r in labels}
    rows = []
    for p in pairs:
        lab = by_no[p['no']]
        pid = pair_id(p['session_id'], p['role'], p['prev_step'], p['cur_step'])
        if pid != lab['pair_id']:
            raise SystemExit(f"pair {p['no']}: pair_id differs from the labels file")
        votes = lab['votes']
        rows.append({'pair_id': pid, 'session_id': p['session_id'], 'scenario_id': scenario_of(p['session_id']),
                     'task': p['task'], 'variant': p['variant'], 'task_type': 'T1', 'mode': 'observe',
                     'session_class': SESSION_CLASS.get(p['termination_reason'], 'other'), 'role': p['role'],
                     'prev_step': p['prev_step'], 'cur_step': p['cur_step'], 'prev': p['prev'], 'cur': p['cur'],
                     'label': lab['label'], 'label_text': lab['label_text'], 'unanimous': lab['unanimous'],
                     'votes': [votes[k] for k in sorted(votes)], 'source': 'nc498', 'nc_no': p['no'],
                     'plan_id': p['plan_id']})
    return rows


def split_sessions(rows, seed=SEED, share=TEST_SHARE, folds=FOLDS):
    """{session: ('test-nc', None) | ('train', fold)} chosen stratum by stratum ((task, variant))."""
    strata = defaultdict(set)
    for r in rows:
        strata[(r['task'], r['variant'])].add(r['session_id'])
    rng = random.Random(seed)
    out, fold_next = {}, 0
    for key in sorted(strata):
        sessions = sorted(strata[key])
        rng.shuffle(sessions)
        k = round(share * len(sessions))
        if k == 0 and len(sessions) >= 2:
            k = 1
        for s in sessions[:k]:
            out[s] = ('test-nc', None)
        for s in sessions[k:]:
            out[s] = ('train', fold_next % folds)
            fold_next += 1
    return out


def build(main_v1, pairs, labels, seed=SEED):
    nc = nc_rows(pairs, labels)
    old_sessions = {r['session_id'] for r in main_v1}
    if old_sessions & {r['session_id'] for r in nc}:
        raise SystemExit('a session is in both main-v1 and nc498')
    plan = split_sessions(nc, seed)
    for r in nc:
        r['split'], r['fold_train'] = plan[r['session_id']]
        r['fold_all'] = None
    rows = [dict(r) for r in main_v1] + nc
    for i, r in enumerate(rows, 1):
        r['no'] = i
    return rows, plan


def check_no_leak(rows):
    where = defaultdict(set)
    for r in rows:
        where[r['session_id']].add((r['split'], r['fold_train']))
    return sorted(s for s, v in where.items() if len(v) > 1)


def summary(rows, plan):
    def count(sel):
        return {'sessions': len({r['session_id'] for r in sel}), 'pairs': len(sel),
                'progress': sum(r['label'] == 1 for r in sel), 'repeat': sum(r['label'] == 0 for r in sel)}
    out = {'seed': SEED, 'splits': {}, 'by_split_task': {}, 'by_split_task_variant': {}}
    for split in ('train', 'test', 'test-nc'):
        sel = [r for r in rows if r['split'] == split]
        out['splits'][split] = count(sel)
        for task in sorted({r['task'] for r in sel}):
            out['by_split_task'][f'{split} {task}'] = count([r for r in sel if r['task'] == task])
        for key in sorted({(r['task'], r['variant']) for r in sel if r['source'] == 'nc498'}):
            out['by_split_task_variant'][f'{split} {key[0]} {key[1]}'] = count(
                [r for r in sel if (r['task'], r['variant']) == key and r['source'] == 'nc498'])
    nc = [r for r in rows if r['source'] == 'nc498']
    test_nc = [r for r in nc if r['split'] == 'test-nc']
    out['nc_test_share'] = {'sessions': round(len({r['session_id'] for r in test_nc}) / len({r['session_id'] for r in nc}), 3),
                            'pairs': round(len(test_nc) / len(nc), 3)}
    out['test_nc_sessions'] = sorted(s for s, (split, _) in plan.items() if split == 'test-nc')
    out['train_folds'] = dict(sorted(Counter(r['fold_train'] for r in rows if r['split'] == 'train').items()))
    out['dialogue_train_pairs'] = sum(r['split'] == 'train' and DOMAIN.get(r['task']) != 'coding' for r in rows)
    out['domain_table_has_nc_tasks'] = all(t in DOMAIN for t in ('DEMO-CS', 'DEMO-MR', 'DEMO-LR'))
    out['session_leaks'] = check_no_leak(rows)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--work', type=Path, default=WORK)
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args(argv)
    main_v1 = load_jsonl(a.work / 'bge-ft' / 'main-v1.jsonl')
    pairs = load_jsonl(a.work / 'nc' / 'nc498-pairs-v0.jsonl')
    labels = load_jsonl(a.work / 'nc' / 'nc498-labels-v1.jsonl')
    rows, plan = build(main_v1, pairs, labels)
    info = summary(rows, plan)
    print(json.dumps(info, ensure_ascii=False, indent=1))
    if info['session_leaks']:
        print('session leak: nothing written', file=sys.stderr)
        return 1
    if not a.dry_run:
        write_jsonl(a.work / 'bge-ft' / 'main-v2.jsonl', rows)
        print(f'wrote {len(rows)} rows', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
