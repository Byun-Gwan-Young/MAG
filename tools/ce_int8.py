"""C-19-12 part 2 : ONNX export -> dynamic INT8 quantization -> fp32 vs INT8 on the same test pairs.

  .../bge-ft/.venv/bin/python -B tools/ce_int8.py --model <runs/student-v2/model> [--out <folder>]

Writes <out>/model-fp32.onnx, <out>/model-int8.onnx (+ the tokenizer) and <out>/result-int8.json : accuracy of each on
'test' and 'test-nc' (overall, dialogue, by domain), how many pairs change their 0.5 decision, file sizes, and CPU
latency with ONNX Runtime at 2 intra-op threads, batch 1, input padded to --max_len (224) tokens : p50 and p99 over
--runs runs after warm-up (the server is 2 x86 vCPUs; a Mac number is a stand-in, DM-102 ④ / DM-113 ④).
Needs torch, transformers, onnx and onnxruntime in the same venv. Reads the test splits only to score them.
"""
import argparse
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.ce_common import WORK, load_jsonl  # noqa: E402
from tools.ce_train import Scorer, evaluate, rel  # noqa: E402


def export(model_dir, out_dir, max_len=224):
    """fp32 ONNX (dynamic batch / sequence axes) and its dynamic INT8 copy; returns both paths."""
    import torch
    from onnxruntime.quantization import QuantType, quantize_dynamic
    from transformers import AutoModelForSequenceClassification, AutoTokenizer
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tok = AutoTokenizer.from_pretrained(str(model_dir))
    model = AutoModelForSequenceClassification.from_pretrained(str(model_dir), dtype=torch.float32).eval()
    model.config.return_dict = False
    tok.save_pretrained(out_dir)
    enc = tok(['a'], ['b'], truncation=True, max_length=max_len, padding='max_length', return_tensors='pt')
    names = [n for n in ('input_ids', 'attention_mask', 'token_type_ids') if n in enc]
    fp32, int8 = out_dir / 'model-fp32.onnx', out_dir / 'model-int8.onnx'
    axes = {n: {0: 'batch', 1: 'sequence'} for n in names} | {'logits': {0: 'batch'}}
    torch.onnx.export(model, tuple(enc[n] for n in names), str(fp32), input_names=names, output_names=['logits'],
                      dynamic_axes=axes, opset_version=17, dynamo=False)
    quantize_dynamic(str(fp32), str(int8), weight_type=QuantType.QInt8)
    return fp32, int8


def latency(path, tok_dir, max_len=224, runs=200, warm=10, threads=2):
    import numpy as np
    import onnxruntime as ort
    from transformers import AutoTokenizer
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = threads
    opts.inter_op_num_threads = 1
    sess = ort.InferenceSession(str(path), opts, providers=['CPUExecutionProvider'])
    tok = AutoTokenizer.from_pretrained(str(tok_dir))
    names = {i.name for i in sess.get_inputs()}
    text = '진전 ' * max_len
    enc = tok([text], [text], truncation='longest_first', max_length=max_len, padding='max_length', return_tensors='np')
    feed = {k: v.astype(np.int64) for k, v in enc.items() if k in names}
    t = []
    for k in range(runs + warm):
        s = time.perf_counter(); sess.run(None, feed); e = (time.perf_counter() - s) * 1000
        if k >= warm:
            t.append(e)
    t.sort()
    return {'tokens': int(feed['input_ids'].shape[1]), 'threads': threads, 'runs': runs,
            'p50': round(statistics.median(t), 2), 'p99': round(t[min(len(t) - 1, int(0.99 * len(t)))], 2)}


def measure(model_dir, data, out_dir, max_len=224, runs=200):
    rows = load_jsonl(data)
    train = [r for r in rows if r['split'] == 'train']
    scored = [r for r in rows if r['split'] in ('test', 'test-nc')]
    fp32, int8 = export(model_dir, out_dir, max_len)
    res = {'model': rel(model_dir), 'max_len': max_len, 'data': Path(data).name}
    probs = {}
    for name, path in (('fp32', fp32), ('int8', int8)):
        probs[name] = Scorer(onnx=path, tokenizer=out_dir, max_len=max_len).probs(scored)
        ev = evaluate(train, scored, probs[name])
        res[name] = {'size_mb': round(path.stat().st_size / 2 ** 20, 1), 'accuracy': ev['accuracy'],
                     'dialogue': ev['dialogue'], 'by_split': ev['by_split'], 'by_domain': ev['by_domain'],
                     'latency_ms_cpu': latency(path, out_dir, max_len, runs)}
    res['decision_changes'] = sum((a >= 0.5) != (b >= 0.5) for a, b in zip(probs['fp32'], probs['int8']))
    res['max_prob_difference'] = round(max((abs(a - b) for a, b in zip(probs['fp32'], probs['int8'])), default=0), 4)
    res['accuracy_difference'] = (None if res['fp32']['accuracy'] is None else
                                  round(res['int8']['accuracy'] - res['fp32']['accuracy'], 4))
    (Path(out_dir) / 'result-int8.json').write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding='utf-8')
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--model', type=Path, required=True)
    ap.add_argument('--data', type=Path, default=WORK / 'bge-ft' / 'main-v2.jsonl')
    ap.add_argument('--out', type=Path, default=WORK / 'bge-ft' / 'runs' / 'student-v2-int8')
    ap.add_argument('--max_len', type=int, default=224)
    ap.add_argument('--runs', type=int, default=200)
    a = ap.parse_args(argv)
    res = measure(a.model, a.data, a.out, a.max_len, a.runs)
    print(json.dumps({k: v for k, v in res.items() if k not in ('fp32', 'int8')} |
                     {n: {k: res[n][k] for k in ('size_mb', 'accuracy', 'latency_ms_cpu')} for n in ('fp32', 'int8')},
                     ensure_ascii=False, indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
