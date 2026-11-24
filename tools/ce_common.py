"""Shared pieces of the cross-encoder model track (C-19-12): pair ids, label names, agreement statistics, the
word-overlap rule and the task domains. No model library is imported here, so the label and split tools run in the
repository venv; the training tools import torch themselves.

Labels : 1 = 진전 (progress), 0 = 같은 주장 반복 (same claim repeated). 판단 보류 (abstain) is no vote.
Data files (pairs with utterance text, labels) live in the git-excluded folder docs/handoff/model-track/ and are never
copied into the repository, logs or reports: the tools print counts and ratios only.
"""
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / 'docs' / 'handoff' / 'model-track'

PROGRESS, REPEAT, ABSTAIN = '진전', '같은 주장 반복', '판단 보류'
LABEL_VALUE = {PROGRESS: 1, REPEAT: 0}
LABEL_TEXT = {1: PROGRESS, 0: REPEAT}

DOMAIN = {'DEMO-CL': 'coding', 'DEMO-CT': 'coding', 'DEMO-CT-N': 'coding', 'DEMO-CS': 'customer-support',
          'DEMO-MR': 'market-research', 'DEMO-LR': 'literature-review'}
DIALOGUE_TASKS = ('DEMO-CS', 'DEMO-MR', 'DEMO-LR')  # the 2nd judgement runs on these only (DM-116 ②)
WORD = re.compile(r'[0-9A-Za-z가-힣_]+')


def pair_id(session_id, role, prev_step, cur_step):
    """The 12-hex id used since main381 (sha256 of 'session|role|prev|cur')."""
    return hashlib.sha256(f'{session_id}|{role}|{prev_step}|{cur_step}'.encode()).hexdigest()[:12]


def domain(task):
    return DOMAIN.get(task, task)


def is_dialogue(task):
    return task in DIALOGUE_TASKS


def word_new(row):
    """Share of the current utterance's words that the previous utterance does not have."""
    cur = set(WORD.findall(row['cur']))
    return len(cur - set(WORD.findall(row['prev']))) / max(1, len(cur))


def fit_word_rule(train):
    """Threshold of word_new fitted on the training pairs only (as in ft_bge.py)."""
    values = sorted({word_new(r) for r in train})
    return max(values, key=lambda t: sum((word_new(r) >= t) == bool(r['label']) for r in train)) if values else 0.5


def load_jsonl(path):
    with open(path, encoding='utf-8') as f:
        return [json.loads(line) for line in f if line.strip()]


def write_jsonl(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')


def cohen_kappa(a, b):
    """Cohen's kappa of two equal-length label lists (no None)."""
    n = len(a)
    if not n:
        return None
    po = sum(x == y for x, y in zip(a, b)) / n
    ca, cb = Counter(a), Counter(b)
    pe = sum(ca[k] * cb[k] for k in set(ca) | set(cb)) / (n * n)
    return 1.0 if pe == 1 else (po - pe) / (1 - pe)


def fleiss_kappa(items):
    """Fleiss' kappa; items is a list of label lists, every item rated by the same number of raters."""
    if not items:
        return None
    m = len(items[0])
    assert all(len(i) == m for i in items) and m > 1
    cats = sorted({x for i in items for x in i})
    n = len(items)
    p_bar = sum((sum(c * c for c in Counter(i).values()) - m) / (m * (m - 1)) for i in items) / n
    totals = Counter(x for i in items for x in i)
    pe = sum((totals[c] / (n * m)) ** 2 for c in cats)
    return 1.0 if pe == 1 else (p_bar - pe) / (1 - pe)


def majority(votes):
    """The label with at least two of the votes (abstain is no vote), else None."""
    count = Counter(v for v in votes if v in LABEL_VALUE)
    top = [k for k, c in count.items() if c >= 2]
    return top[0] if len(top) == 1 else None


def accuracy(rows, predict):
    return round(sum(bool(predict(r)) == bool(r['label']) for r in rows) / len(rows), 4) if rows else None
