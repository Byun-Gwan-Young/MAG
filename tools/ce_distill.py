"""C-19-12 part 2 : distil the teacher into the small student (hf/mMiniLMv2-L6-H384, 6 layers, width 384).

loss = (1 - alpha) * BCE(human label) + alpha * soft(teacher P(진전)), soft = KL of the two Bernoulli distributions
softened by temperature T (x T^2) or MSE of the softened probabilities. The teacher probabilities are the teacher's
5-fold OUT-OF-FOLD predictions on the train split (tools/ce_train.py --eval oof), so the student does not copy scores
the teacher memorised on its own training pairs. Pairs without a teacher probability (e.g. coding pairs when the
teacher trained on dialogue only) take the BCE term only. alpha 0 = plain fine-tuning, no teacher file needed.

Run with the model-track venv, from the repository root :
  .../bge-ft/.venv/bin/python -B tools/ce_distill.py --eval valid --teacher <teacher-oof.json> --alpha 0.5 --temperature 2
  .../bge-ft/.venv/bin/python -B tools/ce_distill.py --eval test  --teacher <teacher-oof.json> --alpha A --temperature T

Choose alpha / T / soft with --eval valid only. Note : on valid, the teacher probabilities of the student's training
pairs came from teachers that trained on the student's validation fold, so the valid number can be slightly
optimistic for alpha > 0 (the test number is not affected). --eval test scores 'test' and 'test-nc' once and saves
the student to <out>/model (input of tools/ce_int8.py).
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.ce_common import WORK  # noqa: E402
from tools.ce_train import add_common, config, run  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    add_common(ap, str(WORK / 'hf' / 'mMiniLMv2-L6-H384'), WORK / 'bge-ft' / 'runs' / 'student-v2')
    ap.add_argument('--teacher', type=Path, help='teacher out-of-fold probabilities (tools/ce_train.py --eval oof)')
    ap.add_argument('--alpha', type=float, default=0.5)
    ap.add_argument('--temperature', type=float, default=2.0)
    ap.add_argument('--soft', choices=['kl', 'mse'], default='kl')
    ap.set_defaults(lr=5e-5, bs=8, accum=2, epochs=4, max_len=224, seed=20261125)
    a = ap.parse_args(argv)
    if a.eval == 'oof':
        ap.error('the student has no oof mode')
    teacher = None
    if a.alpha > 0:
        if not a.teacher:
            ap.error('--alpha > 0 needs --teacher')
        teacher = json.loads(a.teacher.read_text(encoding='utf-8'))['probs']
    cfg = config(a) | {'alpha': a.alpha, 'temperature': a.temperature, 'soft': a.soft}
    run(a, cfg, teacher, kind='student')
    return 0


if __name__ == '__main__':
    sys.exit(main())
