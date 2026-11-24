"""C-19-12 : model-track tools (label settling, data v2 split, out-of-fold predictions, distillation, INT8, replay).

Offline, on small fake data only (no labels, utterances or sessions from docs/handoff/model-track/). The parts that
need torch / transformers (one-epoch teacher and student) or onnx / onnxruntime (INT8 export) are skipped when the
venv lacks them; run this file with the model-track venv to include them :
  docs/handoff/model-track/bge-ft/.venv/bin/python -B -m unittest tests.test_ce_model_track -v
"""
import importlib.util
import json
import random
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import ce_common, ce_data_v2, ce_replay, ce_train, nc_labels  # noqa: E402
from tools.ce_common import ABSTAIN, PROGRESS, REPEAT  # noqa: E402

HAS_TORCH = all(importlib.util.find_spec(m) for m in ('torch', 'transformers', 'tokenizers'))
HAS_ONNX = HAS_TORCH and all(importlib.util.find_spec(m) for m in ('onnx', 'onnxruntime'))

WORDS = ['alpha', 'beta', 'gamma', 'delta', 'eps', 'zeta', 'eta', 'theta', 'iota', 'kappa', 'lam', 'mu']


def fake_rows(sessions=10, per=4, tasks=('DEMO-MR', 'DEMO-CL'), seed=1):
    rng = random.Random(seed)
    rows = []
    for s in range(sessions):
        task = tasks[s % len(tasks)]
        for k in range(per):
            label = rng.randint(0, 1)
            prev = ' '.join(rng.choice(WORDS) for _ in range(6))
            cur = prev if not label else ' '.join(rng.choice(WORDS) for _ in range(6))
            sid = f'{task}-x-v1-{s:04d}'
            rows.append({'pair_id': ce_common.pair_id(sid, 'r', k, k + 3), 'session_id': sid, 'task': task,
                         'variant': 'stall', 'role': 'r', 'prev_step': k, 'cur_step': k + 3, 'prev': prev, 'cur': cur,
                         'label': label, 'split': 'train', 'fold_train': s % 5})
    return rows


class CommonTest(unittest.TestCase):
    def test_pair_id_matches_main381_rule(self):
        self.assertEqual(ce_common.pair_id('S', 'dev', 1, 4), __import__('hashlib').sha256(b'S|dev|1|4').hexdigest()[:12])

    def test_cohen_kappa_known_value(self):
        a = [1, 1, 1, 0, 0, 0, 1, 0]
        b = [1, 1, 0, 0, 0, 0, 1, 1]
        # po = 6/8, pe = 0.5 -> 0.5
        self.assertAlmostEqual(ce_common.cohen_kappa(a, b), 0.5)

    def test_fleiss_kappa_perfect_and_known(self):
        self.assertEqual(ce_common.fleiss_kappa([[1, 1, 1], [0, 0, 0]]), 1.0)
        items = [[1, 1, 0], [1, 1, 1], [0, 0, 0], [0, 1, 0]]
        # P_i = 1/3, 1, 1, 1/3 -> P̄ = 2/3 ; p1 = p0 = 0.5 -> Pe 0.5 -> kappa 1/3
        self.assertAlmostEqual(ce_common.fleiss_kappa(items), 1 / 3)

    def test_majority_ignores_abstain(self):
        self.assertEqual(ce_common.majority([PROGRESS, ABSTAIN, PROGRESS]), PROGRESS)
        self.assertEqual(ce_common.majority([REPEAT, REPEAT, PROGRESS]), REPEAT)
        self.assertIsNone(ce_common.majority([REPEAT, ABSTAIN, PROGRESS]))


class LabelsTest(unittest.TestCase):
    def test_compute_counts_abstain_as_human_difference(self):
        n = nc_labels.PAIRS
        h1 = {i: PROGRESS if i % 4 == 0 else REPEAT for i in range(1, n + 1)}
        h2 = dict(h1)
        ai = dict(h1)
        h2[1] = ABSTAIN
        h2[2] = PROGRESS          # real disagreement
        ai[3] = PROGRESS
        stats, final = nc_labels.compute(h1, h2, ai)
        self.assertEqual(stats['humans_differ'], 2)
        self.assertEqual(stats['humans_differ_both_labelled'], 1)
        self.assertEqual(stats['human_pairs_compared'], n - 1)
        self.assertEqual(stats['undecided'], 0)
        self.assertEqual(final[1], h1[1])  # the abstained pair is decided by the other two
        self.assertEqual(stats['all_three_same'], n - 3)

    def test_mismatch_is_reported(self):
        stats = dict(nc_labels.EXPECTED, humans_differ=12)
        self.assertEqual([m['field'] for m in nc_labels.mismatches(stats)], ['humans_differ'])
        self.assertEqual(nc_labels.mismatches(dict(nc_labels.EXPECTED)), [])


class DataV2Test(unittest.TestCase):
    def nc(self):
        rows = []
        for task in ('DEMO-MR', 'DEMO-LR', 'DEMO-CS'):
            for variant, count in (('stall', 6), ('normal', 4)):
                for s in range(count):
                    for k in range(3):
                        rows.append({'session_id': f'{task}-{variant}-v2-{s}', 'task': task, 'variant': variant})
        return rows

    def test_split_by_session_without_leak_and_every_stratum_tested(self):
        rows = self.nc()
        plan = ce_data_v2.split_sessions(rows)
        self.assertEqual(plan, ce_data_v2.split_sessions(rows))  # fixed seed
        for r in rows:
            r['split'], r['fold_train'] = plan[r['session_id']]
        self.assertEqual(ce_data_v2.check_no_leak(rows), [])
        for task in ('DEMO-MR', 'DEMO-LR', 'DEMO-CS'):
            for variant, count in (('stall', 6), ('normal', 4)):
                self.assertTrue(any(plan[f'{task}-{variant}-v2-{s}'][0] == 'test-nc' for s in range(count)))
        share = sum(v[0] == 'test-nc' for v in plan.values()) / len(plan)
        self.assertTrue(0.15 <= share <= 0.3, share)
        self.assertEqual({v[1] for v in plan.values() if v[0] == 'train'}, set(range(5)))

    def test_leak_detector(self):
        rows = [{'session_id': 'a', 'split': 'train', 'fold_train': 1}, {'session_id': 'a', 'split': 'test-nc', 'fold_train': None}]
        self.assertEqual(ce_data_v2.check_no_leak(rows), ['a'])


class OutOfFoldTest(unittest.TestCase):
    def test_each_pair_predicted_by_a_model_that_did_not_see_its_fold(self):
        rows = fake_rows(sessions=10)
        rows.append(dict(rows[0], pair_id='testpair', split='test', fold_train=None, session_id='T-1'))
        seen = []

        def train_fn(train):
            ids = {r['pair_id'] for r in train}
            seen.append(ids)
            return ids

        def predict_fn(model, held):
            for r in held:
                self.assertNotIn(r['pair_id'], model)
            return [0.5] * len(held)
        probs = ce_train.out_of_fold(rows, train_fn, predict_fn)
        self.assertEqual(len(seen), 5)
        self.assertEqual(set(probs), {r['pair_id'] for r in rows if r['split'] == 'train'})
        self.assertNotIn('testpair', set().union(*seen))

    def test_select_valid_never_reads_test(self):
        rows = fake_rows(sessions=10) + [dict(fake_rows(1)[0], split='test-nc', fold_train=None, session_id='N')]
        for _, train, scored in ce_train.select(rows, 'valid'):
            self.assertTrue(all(r['split'] == 'train' for r in train + scored))
        dia = ce_train.select(rows, 'valid', 0, 'dialogue')[0][1]
        self.assertTrue(all(r['task'] == 'DEMO-MR' for r in dia))


class ReplayTest(unittest.TestCase):
    def session(self, task='DEMO-MR', stops=(5, 6, 7, 8), variant='normal', mode='observe', cls='success'):
        pairs = [{'pair_id': 'p1', 'role': 'a', 'prev_step': 1, 'cur_step': 4},
                 {'pair_id': 'p2', 'role': 'b', 'prev_step': 2, 'cur_step': 5},
                 {'pair_id': 'p3', 'role': 'a', 'prev_step': 4, 'cur_step': 7}]
        return {'session_id': f'{task}-{variant}-{mode}', 'task': task, 'variant': variant, 'mode': mode,
                'class': cls, 'stop_steps': list(stops), 'pairs': pairs, 'total_tokens': 100,
                'cumulative_tokens': {str(i): i * 10 for i in range(1, 11)}}

    def test_cancel_at_most_twice_with_latest_pair_per_role(self):
        s = self.session()
        first, cancelled, _ = ce_replay.effective_stops(s, {'p1': 0.0, 'p2': 0.95, 'p3': 0.0})
        self.assertEqual((first, cancelled), (7, [5, 6]))          # p2 latest for b at 5, 6, 7 ; cap 2
        first, cancelled, _ = ce_replay.effective_stops(s, {'p1': 0.95, 'p2': 0.0, 'p3': 0.0})
        self.assertEqual((first, cancelled), (7, [5, 6]))          # at 7 role a's latest is p3 (0.0)
        first, cancelled, _ = ce_replay.effective_stops(s, {'p1': 0.89, 'p2': 0.0, 'p3': 0.0})
        self.assertEqual((first, cancelled), (5, []))              # under the 0.9 threshold

    def test_coding_and_rule_only_keep_the_stop(self):
        self.assertEqual(ce_replay.effective_stops(self.session(task='DEMO-CL'), {'p2': 1.0})[0], 5)
        self.assertEqual(ce_replay.effective_stops(self.session(), None)[0], 5)

    def test_metrics(self):
        sessions = [self.session(stops=(5,)), self.session(variant='stall', cls='stalled', stops=(6, 9)),
                    self.session(variant='stall', mode='block', cls='stalled', stops=(5,))]
        rule = ce_replay.metrics(sessions, None)
        self.assertEqual((rule['preservation'], rule['detection_observe'], rule['detection_block']), ('0/1', '1/1', '1/1'))
        self.assertAlmostEqual(rule['saving_mean_detected'], 0.4)
        human = ce_replay.metrics(sessions, {'p1': 0.0, 'p2': 1.0, 'p3': 0.0})
        self.assertEqual((human['preservation'], human['detection_block']), ('1/1', '0/1'))
        # stall session : 6 and 9 both see p2 (role b's latest) -> both cancelled, the cap of 2 is used up
        self.assertEqual((human['detection_observe'], human['first_stop_steps']), ('0/1', []))
        self.assertEqual(human['cancels'], 4)


@unittest.skipUnless(HAS_TORCH, 'torch / transformers / tokenizers not in this venv')
class TinyModelTest(unittest.TestCase):
    """One epoch of the teacher and the student on a tiny random BERT with a word-level tokenizer (no download)."""

    @classmethod
    def setUpClass(cls):
        from tokenizers import Tokenizer, models, pre_tokenizers, processors
        from transformers import BertConfig, BertForSequenceClassification, PreTrainedTokenizerFast
        cls.tmp = tempfile.TemporaryDirectory()
        cls.model_dir = Path(cls.tmp.name) / 'tiny'
        special = ['[PAD]', '[UNK]', '[CLS]', '[SEP]']
        vocab = {w: i for i, w in enumerate(special + WORDS + ['진전'])}
        tk = Tokenizer(models.WordLevel(vocab, unk_token='[UNK]'))
        tk.pre_tokenizer = pre_tokenizers.Whitespace()
        tk.post_processor = processors.TemplateProcessing(
            single='[CLS] $A [SEP]', pair='[CLS] $A [SEP] $B:1 [SEP]:1',
            special_tokens=[('[CLS]', vocab['[CLS]']), ('[SEP]', vocab['[SEP]'])])
        tok = PreTrainedTokenizerFast(tokenizer_object=tk, unk_token='[UNK]', pad_token='[PAD]', cls_token='[CLS]',
                                      sep_token='[SEP]', model_max_length=256)
        tok.save_pretrained(cls.model_dir)
        cfg = BertConfig(vocab_size=len(vocab), hidden_size=16, num_hidden_layers=2, num_attention_heads=2,
                         intermediate_size=32, max_position_embeddings=256, num_labels=1)
        BertForSequenceClassification(cfg).save_pretrained(cls.model_dir)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def cfg(self, **kw):
        return {'model': str(self.model_dir), 'epochs': 1, 'lr': 1e-3, 'bs': 4, 'accum': 1, 'max_len': 32,
                'seed': 1, 'device': 'cpu', 'freeze_layers': 0, 'freeze_embeddings': False, 'checkpointing': False} | kw

    def test_teacher_one_epoch_oof_and_student_distillation(self):
        rows = fake_rows(sessions=10)
        trained = ce_train.train_model(rows, self.cfg())
        probs = ce_train.predict(trained, rows[:5])
        self.assertEqual(len(probs), 5)
        self.assertTrue(all(0.0 <= p <= 1.0 for p in probs))
        oof = ce_train.out_of_fold(rows, lambda tr: ce_train.train_model(tr, self.cfg()), ce_train.predict)
        self.assertEqual(set(oof), {r['pair_id'] for r in rows})
        for kind in ('kl', 'mse'):
            student = ce_train.train_model(rows, self.cfg(alpha=0.5, temperature=2.0, soft=kind), teacher=oof)
            self.assertEqual(len(student['log']), 1)
            self.assertTrue(student['log'][0]['loss'] == student['log'][0]['loss'])  # not NaN

    def test_soft_loss_is_zero_when_student_matches_teacher(self):
        import torch
        z = torch.tensor([0.3, -1.2])
        kl = ce_train.soft_loss(z, torch.sigmoid(z), 2.0, 'kl')
        self.assertAlmostEqual(float(kl), 0.0, places=5)

    @unittest.skipUnless(HAS_ONNX, 'onnx / onnxruntime not in this venv')
    def test_int8_export_and_score(self):
        from tools import ce_int8
        with tempfile.TemporaryDirectory() as out:
            fp32, int8 = ce_int8.export(self.model_dir, out, max_len=32)
            self.assertTrue(fp32.is_file() and int8.is_file())
            rows = fake_rows(sessions=2)
            a = ce_train.Scorer(onnx=fp32, tokenizer=out, max_len=32).probs(rows)
            b = ce_train.Scorer(onnx=int8, tokenizer=out, max_len=32).probs(rows)
            self.assertEqual(len(a), len(b))
            lat = ce_int8.latency(int8, out, max_len=32, runs=5, warm=1)
            self.assertEqual(lat['tokens'], 32)


if __name__ == '__main__':
    unittest.main()
