"""C-19-12 part 2 : cross-encoder teacher (ft_bge.py v2), the shared training loop, out-of-fold predictions and a scorer.

Run with the model-track venv (torch + transformers), from the repository root, e.g.
  docs/handoff/model-track/bge-ft/.venv/bin/python -B tools/ce_train.py --eval valid --data docs/handoff/model-track/bge-ft/main-v2.jsonl

  --eval valid   5-fold inside the train split (fold k scored by a model trained on the other folds; --fold k = one
                 fold only). Choose settings with this; repeat freely. The test splits are never read.
  --eval test    train on the whole train split, score 'test' (the 80 old pairs) and 'test-nc' ONCE; saves the model.
  --eval oof     out-of-fold P(진전) for every train pair (each fold by a model that did not train on it) -> the
                 teacher probabilities for distillation (tools/ce_distill.py). No test pair is read.
  --train-domain all | dialogue   dialogue = train on CS · MR · LR pairs only (the 2nd judgement runs on these only)

Writes <out>/result-<eval>.json : accuracy overall / by domain / dialogue, the word-overlap rule fitted on the same
training pairs and scored on the same pairs, latency (device and CPU 2 threads, batch 1). Never prints utterances.
Labels : 1 = 진전, 0 = 같은 주장 반복. Input : (current, previous) utterance, truncated to --max_len tokens.
"""
import argparse
import json
import math
import random
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.ce_common import WORK, accuracy, domain, fit_word_rule, is_dialogue, load_jsonl, word_new  # noqa: E402

FOLDS = 5


# ---------------------------------------------------------------- data selection (no torch)

def select(rows, eval_mode, fold=None, train_domain='all'):
    """[(name, train rows, scored rows)] for one evaluation. Test rows are only touched by eval_mode 'test'."""
    keep = (lambda r: is_dialogue(r['task'])) if train_domain == 'dialogue' else (lambda r: True)
    train = [r for r in rows if r['split'] == 'train' and keep(r)]
    if eval_mode == 'test':
        return [('test', train, [r for r in rows if r['split'] in ('test', 'test-nc')])]
    folds = [fold] if fold is not None else list(range(FOLDS))
    return [(f'fold{k}', [r for r in train if r['fold_train'] != k], [r for r in train if r['fold_train'] == k])
            for k in folds]


def out_of_fold(rows, train_fn, predict_fn, train_domain='all'):
    """pair_id -> probability, each train pair predicted by a model trained without its fold."""
    probs = {}
    for name, train, held in select(rows, 'oof', None, train_domain):
        held_ids = {r['pair_id'] for r in held}
        assert not held_ids & {r['pair_id'] for r in train}, f'{name}: fold leak'
        assert not {r['session_id'] for r in held} & {r['session_id'] for r in train}, f'{name}: session leak'
        model = train_fn(train)
        for r, p in zip(held, predict_fn(model, held)):
            probs[r['pair_id']] = p
    return probs


def evaluate(train, scored, probs):
    """Accuracy of the model, by domain, on dialogue pairs, and of the word rule fitted on `train`."""
    thr = fit_word_rule(train)
    P = {id(r): p for r, p in zip(scored, probs)}
    model_ok = lambda r: P[id(r)] >= 0.5  # noqa: E731
    rule_ok = lambda r: word_new(r) >= thr  # noqa: E731
    res = {'pairs': len(scored), 'accuracy': accuracy(scored, model_ok),
           'word_overlap_rule': {'threshold': round(thr, 4), 'accuracy': accuracy(scored, rule_ok)},
           'dialogue': {}, 'by_domain': {}, 'by_split': {}}
    dia = [r for r in scored if is_dialogue(r['task'])]
    res['dialogue'] = {'pairs': len(dia), 'model': accuracy(dia, model_ok), 'rule': accuracy(dia, rule_ok)}
    for d in sorted({domain(r['task']) for r in scored}):
        sel = [r for r in scored if domain(r['task']) == d]
        res['by_domain'][d] = {'pairs': len(sel), 'model': accuracy(sel, model_ok), 'rule': accuracy(sel, rule_ok)}
    for s in sorted({r['split'] for r in scored}):
        sel = [r for r in scored if r['split'] == s]
        sdia = [r for r in sel if is_dialogue(r['task'])]
        res['by_split'][s] = {'pairs': len(sel), 'model': accuracy(sel, model_ok), 'rule': accuracy(sel, rule_ok),
                              'dialogue_pairs': len(sdia), 'dialogue_model': accuracy(sdia, model_ok),
                              'dialogue_rule': accuracy(sdia, rule_ok)}
    return res


def pool(results):
    """Pool fold results by pair counts (the 5-fold number)."""
    n = sum(r['pairs'] for r in results)

    def avg(get):
        vals = [(get(r), r['pairs']) for r in results if get(r) is not None]
        return round(sum(v * w for v, w in vals) / sum(w for _, w in vals), 4) if vals else None
    out = {'pairs': n, 'accuracy': avg(lambda r: r['accuracy']),
           'word_overlap_rule_accuracy': avg(lambda r: r['word_overlap_rule']['accuracy'])}
    dn = sum(r['dialogue']['pairs'] for r in results)
    out['dialogue'] = {'pairs': dn,
                       'model': round(sum((r['dialogue']['model'] or 0) * r['dialogue']['pairs'] for r in results) / dn, 4) if dn else None,
                       'rule': round(sum((r['dialogue']['rule'] or 0) * r['dialogue']['pairs'] for r in results) / dn, 4) if dn else None}
    return out


# ---------------------------------------------------------------- torch part

def device(name='auto'):
    import torch
    if name != 'auto':
        return torch.device(name)
    if torch.cuda.is_available():
        return torch.device('cuda')
    if getattr(torch.backends, 'mps', None) and torch.backends.mps.is_available():
        return torch.device('mps')
    return torch.device('cpu')


def encode(tok, rows, max_len, pad_to_max=False):
    return tok([r['cur'] for r in rows], [r['prev'] for r in rows], truncation='longest_first', max_length=max_len,
               padding='max_length' if pad_to_max else True, return_tensors='pt')


def load_model(name, num_labels=1):
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer
    tok = AutoTokenizer.from_pretrained(name)
    model = AutoModelForSequenceClassification.from_pretrained(name, dtype=torch.float32)  # fp16 checkpoints -> fp32
    if model.config.num_labels not in (1, 2):
        model = AutoModelForSequenceClassification.from_pretrained(name, num_labels=num_labels, dtype=torch.float32,
                                                                   ignore_mismatched_sizes=True)
    return tok, model


def logit_progress(model, logits):
    import torch
    return logits[:, 0] if model.config.num_labels == 1 else logits[:, 1] - logits[:, 0]


def soft_loss(student_logit, teacher_prob, temperature, kind):
    """Distillation term against the teacher's P(진전), both softened by the temperature."""
    import torch
    t = teacher_prob.clamp(1e-6, 1 - 1e-6)
    t_soft = torch.sigmoid(torch.logit(t) / temperature)
    s_logit = student_logit / temperature
    if kind == 'mse':
        return torch.nn.functional.mse_loss(torch.sigmoid(s_logit), t_soft)
    # KL(teacher || student) of two Bernoulli distributions, scaled by T^2 (Hinton et al.)
    log_s1 = torch.nn.functional.logsigmoid(s_logit)
    log_s0 = torch.nn.functional.logsigmoid(-s_logit)
    kl = t_soft * (torch.log(t_soft) - log_s1) + (1 - t_soft) * (torch.log(1 - t_soft) - log_s0)
    return kl.mean() * temperature ** 2


def train_model(train, cfg, teacher=None):
    """Fine-tune cfg['model'] on `train`. With teacher (pair_id -> prob) and cfg['alpha'] > 0 the loss is
    (1 - alpha) * BCE(label) + alpha * soft_loss(teacher); pairs without a teacher prob take the BCE only."""
    import torch
    from transformers import get_linear_schedule_with_warmup
    rng = random.Random(cfg['seed'])
    torch.manual_seed(cfg['seed'])
    dev = device(cfg.get('device', 'auto'))
    tok, model = load_model(cfg['model'])
    if cfg.get('freeze_embeddings'):
        for p in model.base_model.embeddings.parameters():
            p.requires_grad = False
    for layer in model.base_model.encoder.layer[:cfg.get('freeze_layers', 0)]:
        for p in layer.parameters():
            p.requires_grad = False
    if cfg.get('checkpointing', True):
        model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={'use_reentrant': False})
        model.config.use_cache = False
    model.to(dev)
    alpha = cfg.get('alpha', 0.0) if teacher else 0.0
    opt = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad], lr=cfg['lr'], weight_decay=0.01)
    steps = max(1, math.ceil(len(train) / (cfg['bs'] * cfg['accum'])) * cfg['epochs'])
    sch = get_linear_schedule_with_warmup(opt, int(0.1 * steps), steps)
    order, t0, log = list(train), time.time(), []
    for ep in range(cfg['epochs']):
        model.train()
        rng.shuffle(order)
        total = 0.0
        for i in range(0, len(order), cfg['bs']):
            b = order[i:i + cfg['bs']]
            enc = {k: v.to(dev) for k, v in encode(tok, b, cfg['max_len']).items()}
            y = torch.tensor([float(r['label']) for r in b], device=dev)
            z = logit_progress(model, model(**enc).logits)
            loss = torch.nn.functional.binary_cross_entropy_with_logits(z, y)
            if alpha:
                has = [k for k, r in enumerate(b) if r['pair_id'] in teacher]
                if has:
                    tp = torch.tensor([teacher[b[k]['pair_id']] for k in has], device=dev)
                    soft = soft_loss(z[has], tp, cfg['temperature'], cfg['soft'])
                    loss = (1 - alpha) * loss + alpha * soft * len(has) / len(b)
            (loss / cfg['accum']).backward()
            total += loss.item() * len(b)
            if (i // cfg['bs'] + 1) % cfg['accum'] == 0 or i + cfg['bs'] >= len(order):
                torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                opt.step(); sch.step(); opt.zero_grad()
        log.append({'epoch': ep + 1, 'loss': round(total / max(1, len(order)), 4), 'seconds': round(time.time() - t0)})
        print(f"epoch {ep + 1}/{cfg['epochs']} loss {log[-1]['loss']} · {log[-1]['seconds']}s", flush=True)
    model.eval()
    return {'tok': tok, 'model': model, 'device': dev, 'max_len': cfg['max_len'], 'log': log}


def predict(trained, rows, batch=8):
    import torch
    tok, model, dev, max_len = trained['tok'], trained['model'], trained['device'], trained['max_len']
    out = []
    with torch.no_grad():
        for i in range(0, len(rows), batch):
            enc = {k: v.to(dev) for k, v in encode(tok, rows[i:i + batch], max_len).items()}
            out += torch.sigmoid(logit_progress(model, model(**enc).logits)).float().cpu().tolist()
    return out


def latency_torch(trained, sample, runs=23, warm=3):
    import torch
    res = {}
    for name, dev, threads in ((str(trained['device']), trained['device'], None), ('cpu-2-threads', torch.device('cpu'), 2)):
        if threads:
            torch.set_num_threads(threads)
        m = trained['model'].to(dev)
        enc = {k: v.to(dev) for k, v in encode(trained['tok'], sample, trained['max_len'], pad_to_max=True).items()}
        t = []
        with torch.no_grad():
            for k in range(runs):
                s = time.perf_counter(); m(**enc).logits.float().cpu(); e = (time.perf_counter() - s) * 1000
                if k >= warm:
                    t.append(e)
        t.sort()
        res[name] = {'tokens': int(enc['input_ids'].shape[1]), 'p50': round(statistics.median(t), 1),
                     'max': round(t[-1], 1), 'runs': len(t), 'note': 'fp32 PyTorch, batch 1'}
    trained['model'].to(trained['device'])
    return res


class Scorer:
    """P(진전) from a saved Hugging Face model folder (torch) or an ONNX file (onnxruntime) with its tokenizer."""

    def __init__(self, model=None, onnx=None, tokenizer=None, max_len=224, threads=None):
        from transformers import AutoTokenizer
        self.max_len = max_len
        if onnx:
            import onnxruntime as ort
            opts = ort.SessionOptions()
            if threads:
                opts.intra_op_num_threads = threads
            self.session = ort.InferenceSession(str(onnx), opts, providers=['CPUExecutionProvider'])
            self.inputs = {i.name for i in self.session.get_inputs()}
            self.tok = AutoTokenizer.from_pretrained(str(tokenizer or Path(onnx).parent))
            self.trained = None
        else:
            import torch
            from transformers import AutoModelForSequenceClassification
            self.tok = AutoTokenizer.from_pretrained(str(model))
            m = AutoModelForSequenceClassification.from_pretrained(str(model), dtype=torch.float32).eval()
            dev = device('auto')
            self.trained = {'tok': self.tok, 'model': m.to(dev), 'device': dev, 'max_len': max_len}

    def probs(self, rows, batch=8):
        if self.trained:
            return predict(self.trained, rows, batch)
        import numpy as np
        out = []
        for i in range(0, len(rows), batch):
            b = rows[i:i + batch]
            enc = self.tok([r['cur'] for r in b], [r['prev'] for r in b], truncation='longest_first',
                           max_length=self.max_len, padding=True, return_tensors='np')
            feed = {k: v.astype(np.int64) for k, v in enc.items() if k in self.inputs}
            logits = self.session.run(None, feed)[0]
            z = logits[:, 0] if logits.shape[1] == 1 else logits[:, 1] - logits[:, 0]
            out += (1 / (1 + np.exp(-z))).astype(float).tolist()
        return out


# ---------------------------------------------------------------- CLI

def config(a):
    return {k: getattr(a, k) for k in ('model', 'epochs', 'lr', 'bs', 'accum', 'max_len', 'seed', 'device',
                                       'freeze_layers', 'freeze_embeddings')} | {'checkpointing': not a.no_checkpointing}


def add_common(ap, model, out):
    ap.add_argument('--model', default=model)
    ap.add_argument('--data', type=Path, default=WORK / 'bge-ft' / 'main-v2.jsonl')
    ap.add_argument('--eval', choices=['valid', 'test', 'oof'], required=True)
    ap.add_argument('--fold', type=int, help='valid : score this fold only (default: all 5)')
    ap.add_argument('--train-domain', choices=['all', 'dialogue'], default='all')
    ap.add_argument('--epochs', type=int, default=3)
    ap.add_argument('--lr', type=float, default=1e-5)
    ap.add_argument('--bs', type=int, default=2)
    ap.add_argument('--accum', type=int, default=8)
    ap.add_argument('--max_len', type=int, default=256)
    ap.add_argument('--seed', type=int, default=20261112)
    ap.add_argument('--out', type=Path, default=out)
    ap.add_argument('--freeze_layers', type=int, default=0)
    ap.add_argument('--freeze_embeddings', action='store_true')
    ap.add_argument('--device', choices=['auto', 'cpu', 'mps', 'cuda'], default='auto')
    ap.add_argument('--no_checkpointing', action='store_true')
    ap.add_argument('--tag', default='', help='suffix of the result file name')


def rel(value):
    """Paths inside the repository as repository-relative text (no home folder in result files)."""
    text = str(value)
    root = str(ROOT) + '/'
    return text[len(root):] if text.startswith(root) else text


def run(a, cfg, teacher=None, kind='teacher'):
    rows = load_jsonl(a.data)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    suffix = f'-{a.tag}' if a.tag else ''
    base = {'kind': kind, 'model': rel(cfg['model']), 'eval': a.eval, 'train_domain': a.train_domain,
            'settings': {k: rel(v) if isinstance(v, (Path, str)) else v for k, v in vars(a).items()},
            'data': str(a.data.name)}
    if a.eval == 'oof':
        probs = out_of_fold(rows, lambda tr: train_model(tr, cfg, teacher), predict, a.train_domain)
        (out / f'{kind}-oof{suffix}.json').write_text(json.dumps({'probs': probs, **base}, indent=0), encoding='utf-8')
        print(json.dumps({'oof_pairs': len(probs)}))
        return base | {'oof_pairs': len(probs)}
    results, folds, t0 = [], [], time.time()
    for name, train, scored in select(rows, a.eval, a.fold, a.train_domain):
        assert not {r['session_id'] for r in train} & {r['session_id'] for r in scored}, 'session leak'
        print(f'{name} : train {len(train)} · scored {len(scored)}', flush=True)
        trained = train_model(train, cfg, teacher)
        res = evaluate(train, scored, predict(trained, scored))
        res['name'], res['train_pairs'], res['log'] = name, len(train), trained['log']
        folds.append(res)
        if a.eval == 'test':
            trained['model'].save_pretrained(out / 'model')
            trained['tok'].save_pretrained(out / 'model')
            base['latency_ms'] = latency_torch(trained, scored[:1])
    base['train_seconds'] = round(time.time() - t0)
    base['folds'] = folds
    if a.eval == 'valid':
        base['pooled'] = pool(folds)
    result = base
    (out / f'result-{a.eval}{suffix}.json').write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding='utf-8')
    print(json.dumps({k: v for k, v in result.items() if k not in ('settings', 'folds')} |
                     {'folds': [{k: f[k] for k in ('name', 'pairs', 'accuracy', 'dialogue')} for f in folds]},
                     ensure_ascii=False, indent=1))
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    add_common(ap, 'BAAI/bge-reranker-v2-m3', WORK / 'bge-ft' / 'runs' / 'teacher-v2')
    a = ap.parse_args(argv)
    run(a, config(a))
    return 0


if __name__ == '__main__':
    sys.exit(main())
