"""Same-role consecutive utterance pairs of demo sessions, for progress labelling (DM-102 1, DM-117).

A pair is (previous, current) narrative of the same role in one session: the narrative field of each step's final
report (steps.json text). reasoning_content is never read. Steps whose report does not parse give no utterance and
do not break the pair chain of that role.

  extract_pairs.py --plans nc-main-v2 nc-main-v3-cs --out pairs.jsonl     sessions whose batch plan_id is listed
  extract_pairs.py --prefix nc- --out pairs.jsonl                          plan_id starting with the prefix

Each line: session_id, plan_id, item, task, variant, termination_reason, role, prev_step, cur_step, prev, cur.
plan_id, item, variant and termination_reason are for the PM only (labellers must not see them). Offline.
"""
import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FENCE = re.compile(r'```(?:json)?\s*(\{.*\})\s*```', re.S)


def narrative(text):
    if not isinstance(text, str):
        return None
    m = FENCE.search(text)
    try:
        value = json.loads(m.group(1) if m else text.strip())
    except ValueError:
        return None
    n = value.get('narrative') if isinstance(value, dict) else None
    return n.strip() if isinstance(n, str) and n.strip() else None


def session_pairs(folder):
    steps = json.loads((folder / 'steps.json').read_text(encoding='utf-8'))
    last, rows, unparsed = {}, [], 0
    for step in steps:
        n = narrative(step.get('text'))
        if n is None:
            unparsed += 1
            continue
        role = step.get('agent')
        if role in last:
            prev_step, prev = last[role]
            rows.append({'role': role, 'prev_step': prev_step, 'cur_step': step['step_id'], 'prev': prev, 'cur': n})
        last[role] = (step['step_id'], n)
    return rows, unparsed


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--runs', type=Path, default=ROOT / 'runs/demo')
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument('--plans', nargs='+')
    group.add_argument('--prefix')
    ap.add_argument('--out', type=Path, required=True)
    a = ap.parse_args(argv)
    out, stats = [], {'sessions': 0, 'unparsed_steps': 0}
    for folder in sorted(p for p in a.runs.iterdir() if (p / 'steps.json').is_file() and (p / 'conditions.json').is_file()):
        batch = json.loads((folder / 'conditions.json').read_text(encoding='utf-8')).get('batch') or {}
        plan = batch.get('plan_id') or ''
        if not ((a.plans and plan in a.plans) or (a.prefix and plan.startswith(a.prefix))):
            continue
        summary = folder / 'summary.json'
        reason = json.loads(summary.read_text(encoding='utf-8')).get('termination_reason') if summary.is_file() else None
        scenario = batch.get('item', '').split(':')[0]
        rows, unparsed = session_pairs(folder)
        stats['sessions'] += 1
        stats['unparsed_steps'] += unparsed
        for r in rows:
            out.append({'session_id': folder.name, 'plan_id': plan, 'item': batch.get('item'),
                        'task': re.sub(r'-(normal|stall)-v\d+$', '', scenario),
                        'variant': 'stall' if '-stall-' in scenario else 'normal', 'termination_reason': reason, **r})
    a.out.parent.mkdir(parents=True, exist_ok=True)
    with a.out.open('w', encoding='utf-8') as f:
        for r in out:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')
    stats['pairs'] = len(out)
    print(json.dumps(stats))
    return stats


if __name__ == '__main__':
    main()
