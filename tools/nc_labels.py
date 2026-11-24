"""C-19-12 part 0 : settle the labels of the 498 non-coding pairs (DM-123).

Reads the two labellers' workbooks (sheet 라벨, from row 5, the 예시 row skipped, 번호 1..498), the AI labels
(AI라벨498_PM전용.json) and the pairs file (nc498-pairs-v0.jsonl, for pair_id), recomputes the agreement numbers and
checks them against DM-123, then writes the majority labels (two of three; 판단 보류 is no vote) to
nc/nc498-labels-v1.jsonl : 번호 · pair_id · 정답 · the three votes. Prints counts and kappas only, never text.

  venv/bin/python -B tools/nc_labels.py                 check + write (exit 1 and nothing written if a number differs)
  venv/bin/python -B tools/nc_labels.py --check-only
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.ce_common import (ABSTAIN, LABEL_VALUE, PROGRESS, REPEAT, WORK, cohen_kappa, fleiss_kappa,  # noqa: E402
                             load_jsonl, majority, pair_id, write_jsonl)

LABELLERS = ('성상혁', '이정은')
PAIRS = 498
# DM-123 (PM, 11/25) : what the recomputation must give back
EXPECTED = {'human_kappa': 0.929, 'human_ai_kappa': [0.929, 0.928], 'fleiss_kappa': 0.928, 'all_three_same': 479,
            'humans_differ': 13, 'majority': {PROGRESS: 106, REPEAT: 392}, 'undecided': 0}


def read_workbook(path):
    import openpyxl
    ws = openpyxl.load_workbook(path, read_only=True, data_only=True)['라벨']
    out = {}
    for row in ws.iter_rows(min_row=5, values_only=True):
        if not row or row[0] is None or row[0] == '예시':
            continue
        no, label = int(row[0]), row[5]
        label = label.strip() if isinstance(label, str) else None
        if label not in (PROGRESS, REPEAT, ABSTAIN):
            raise SystemExit(f'{path.name}: 번호 {no} has no valid label')
        out[no] = label
    if sorted(out) != list(range(1, PAIRS + 1)):
        raise SystemExit(f'{path.name}: 번호 is not exactly 1..{PAIRS}')
    return out


def compute(h1, h2, ai):
    nos = range(1, PAIRS + 1)
    both = [n for n in nos if ABSTAIN not in (h1[n], h2[n])]
    stats = {'pairs': PAIRS, 'abstain': {LABELLERS[0]: sum(h1[n] == ABSTAIN for n in nos),
                                          LABELLERS[1]: sum(h2[n] == ABSTAIN for n in nos),
                                          'ai': sum(ai[n] == ABSTAIN for n in nos)},
             'human_pairs_compared': len(both),
             'human_agreement': round(sum(h1[n] == h2[n] for n in both) / len(both), 4),
             'human_kappa': round(cohen_kappa([h1[n] for n in both], [h2[n] for n in both]), 3)}
    stats['human_ai_kappa'] = []
    for h in (h1, h2):
        sel = [n for n in nos if ABSTAIN not in (h[n], ai[n])]
        stats['human_ai_kappa'].append(round(cohen_kappa([h[n] for n in sel], [ai[n] for n in sel]), 3))
    full = [n for n in nos if ABSTAIN not in (h1[n], h2[n], ai[n])]
    stats['fleiss_pairs'] = len(full)
    stats['fleiss_kappa'] = round(fleiss_kappa([[h1[n], h2[n], ai[n]] for n in full]), 3)
    stats['all_three_same'] = sum(h1[n] == h2[n] == ai[n] for n in nos)
    # DM-123 counts the abstained pair as a difference (13 = 12 different labels + 1 판단 보류)
    stats['humans_differ'] = sum(h1[n] != h2[n] for n in nos)
    stats['humans_differ_both_labelled'] = sum(h1[n] != h2[n] for n in both)
    final = {n: majority([h1[n], h2[n], ai[n]]) for n in nos}
    stats['majority'] = {PROGRESS: sum(v == PROGRESS for v in final.values()),
                         REPEAT: sum(v == REPEAT for v in final.values())}
    stats['undecided'] = sum(v is None for v in final.values())
    return stats, final


def mismatches(stats):
    bad = []
    for key, want in EXPECTED.items():
        if stats[key] != want:
            bad.append({'field': key, 'expected': want, 'got': stats[key]})
    return bad


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--work', type=Path, default=WORK / 'nc')
    ap.add_argument('--check-only', action='store_true')
    a = ap.parse_args(argv)
    h1, h2 = (read_workbook(a.work / f'비코딩라벨498_라벨({name}).xlsx') for name in LABELLERS)
    ai = {int(r['no']): r['label'] for r in json.loads((a.work / 'AI라벨498_PM전용.json').read_text(encoding='utf-8'))}
    if sorted(ai) != list(range(1, PAIRS + 1)):
        raise SystemExit('AI labels: 번호 is not exactly 1..498')
    pairs = {int(r['no']): r for r in load_jsonl(a.work / 'nc498-pairs-v0.jsonl')}
    if sorted(pairs) != list(range(1, PAIRS + 1)):
        raise SystemExit('nc498-pairs-v0.jsonl: no is not exactly 1..498')
    stats, final = compute(h1, h2, ai)
    bad = mismatches(stats)
    print(json.dumps({'stats': stats, 'dm123_mismatches': bad}, ensure_ascii=False, indent=1))
    if bad:
        print('DM-123 numbers differ: nothing written (stop and report).', file=sys.stderr)
        return 1
    if a.check_only:
        return 0
    rows = []
    for n in range(1, PAIRS + 1):
        p = pairs[n]
        rows.append({'no': n, 'pair_id': pair_id(p['session_id'], p['role'], p['prev_step'], p['cur_step']),
                     'label': LABEL_VALUE[final[n]], 'label_text': final[n],
                     'votes': {LABELLERS[0]: h1[n], LABELLERS[1]: h2[n], 'ai': ai[n]},
                     'unanimous': h1[n] == h2[n] == ai[n]})
    out = a.work / 'nc498-labels-v1.jsonl'
    write_jsonl(out, rows)
    print(f"wrote {len(rows)} rows to {out}", file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
