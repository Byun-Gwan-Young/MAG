"""DEMO-CS v3 (DM-118): the two process tools, their order per variant, the v3 judge and the scenario files."""
import json
from pathlib import Path
import unittest

from experiments import cs_tools, dialogue_tools
from experiments.tool_results import ToolFactError
from scenario_schema import load_scenario

ROOT = Path(__file__).resolve().parents[1]


def judge_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location('cs_v3_judge', ROOT / 'scenario_judges/DEMO-CS-v3.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


class Flow(unittest.TestCase):
    def dispatch(self, variant):
        return dialogue_tools.ToolDispatch(dialogue_tools.IMPLEMENTATIONS, ROOT / f'scenario_assets/DEMO-CS-{variant}-v3')

    def call(self, d, agent, name, **arguments):
        d.agent_id, d.step_id = agent, 1
        try:
            return json.loads(d.call(name, arguments))
        except ToolFactError as error:
            return 'error: ' + str(error)

    def test_normal_report_then_refund(self):
        d = self.dispatch('normal')
        self.assertIn('error', self.call(d, 'refund', 'register_refund', order_id='B-2041', amount_krw=12000))
        self.assertEqual(self.call(d, 'shipping', 'issue_damage_report', order_id='B-2041')['damage_report_id'], 'DR-B-2041')
        self.assertEqual(self.call(d, 'refund', 'register_refund', order_id='B-2041', amount_krw=12000)['refund_id'], 'RF-B-2041')

    def test_stall_waits_both_ways(self):
        d = self.dispatch('stall')
        for _ in range(3):
            self.assertIn('환불 등록', self.call(d, 'shipping', 'issue_damage_report', order_id='B-2041'))
            self.assertIn('파손 확인서', self.call(d, 'refund', 'register_refund', order_id='B-2041', amount_krw=12000))

    def test_bad_arguments(self):
        d = self.dispatch('normal')
        self.assertIn('error', self.call(d, 'shipping', 'issue_damage_report', order_id='X-1'))
        self.call(d, 'shipping', 'issue_damage_report', order_id='B-2041')
        self.assertIn('error', self.call(d, 'refund', 'register_refund', order_id='B-2041', amount_krw=999999))

    def test_inputs(self):
        for v in ('normal', 'stall'):
            for t in cs_tools.TOOLS:
                self.assertEqual(cs_tools.input_problems(t, ROOT / f'scenario_assets/DEMO-CS-{v}-v3'), [], (v, t))

    def test_instruction_unchanged_for_other_roles(self):
        self.assertNotIn('register_refund', dialogue_tools.tool_instructions(['web_search']))
        self.assertIn('register_refund', dialogue_tools.tool_instructions(['read_policy', 'register_refund']))


class Judge(unittest.TestCase):
    def report(self, refund_id='RF-B-2041', code='PARTIAL_REFUND'):
        return {'status': 'resolved', 'deliverable': {'order_id': 'B-2041', 'handling_code': code, 'owner': 'refund',
                                                      'customer_reply': '부분 환불 12,000원', 'refund_id': refund_id}}

    def run_judge(self, report, records):
        owner = [{'agent_id': 'refund', 'deliverable': {'handling_code': report['deliverable']['handling_code']}}]
        return judge_module().judge(report, tool_records=records, inputs_dir=ROOT / 'scenario_assets/DEMO-CS-normal-v3',
                                    reports=owner)

    def test_needs_a_registered_refund(self):
        ok = [{'tool': 'register_refund', 'is_error': False, 'result': json.dumps({'refund_id': 'RF-B-2041', 'order_id': 'B-2041'})}]
        self.assertTrue(self.run_judge(self.report(), ok)['machine_success'])
        self.assertIn('refund_id', self.run_judge(self.report(), [])['missing'])
        self.assertIn('refund_id', self.run_judge(self.report('RF-made-up'), ok)['missing'])
        self.assertIn('handling_code', self.run_judge(self.report(code='INFO_ONLY'), ok)['missing'])


class Scenarios(unittest.TestCase):
    def test_load_and_pair(self):
        a = load_scenario(ROOT / 'scenarios/DEMO-CS-normal-v3.yaml')
        b = load_scenario(ROOT / 'scenarios/DEMO-CS-stall-v3.yaml')
        self.assertEqual([x.tools for x in a.agents], [x.tools for x in b.agents])
        self.assertIn('register_refund', [x for ag in a.agents if ag.id == 'refund' for x in ag.tools])
        self.assertIn('issue_damage_report', [x for ag in a.agents if ag.id == 'shipping' for x in ag.tools])
        self.assertEqual(a.task, b.task)
        self.assertEqual(dialogue_tools.missing_implementations(a), [])


if __name__ == '__main__':
    unittest.main()
