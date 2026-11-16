"""Completion judge of DEMO-CS v3 (DM-118), shared by DEMO-CS-normal-v3 and DEMO-CS-stall-v3 (DM-15: the pair differs
in one shipping-policy sentence and the matching process.json flag). Agents cannot read it (scenario_judges/).

Everything DEMO-CS-v1 checks, plus the refund itself: the ticket asks for a partial refund, so
- handling_code is PARTIAL_REFUND or FULL_REFUND, and
- deliverable.refund_id is the refund_id of a successful register_refund call of this session for the same order
  (tool_records). A number the agents write without the call does not count.
So the ticket closes only through the two process tools (experiments/cs_tools.py), whose order the variant sets.
"""
import importlib.util
import json
from pathlib import Path

_spec = importlib.util.spec_from_file_location('demo_cs_v1_judge', Path(__file__).with_name('DEMO-CS-v1.py'))
_v1 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_v1)

REFUND_CODES = ('PARTIAL_REFUND', 'FULL_REFUND')


def registered_refunds(tool_records, order_id):
    out = set()
    for record in tool_records or ():
        if record.get('tool') != 'register_refund' or record.get('is_error') or not record.get('result'):
            continue
        try:
            value = json.loads(record['result'])
        except ValueError:
            continue
        if isinstance(value, dict) and value.get('order_id') == order_id and value.get('refund_id'):
            out.add(value['refund_id'])
    return out


def judge(report, *, tool_records, inputs_dir, reports=()):
    gate = _v1.judge(report, tool_records=tool_records, inputs_dir=inputs_dir, reports=reports)
    if gate['result'] == 'escalated' or 'deliverable' in gate['missing']:
        return gate
    deliverable = (report or {}).get('deliverable') or {}
    missing = list(gate['missing'])
    if deliverable.get('handling_code') not in REFUND_CODES and 'handling_code' not in missing:
        missing.append('handling_code')
    if deliverable.get('refund_id') not in registered_refunds(tool_records, deliverable.get('order_id')):
        missing.append('refund_id')
    return {'machine_success': not missing, 'result': 'unfinished' if missing else 'resolved', 'missing': missing}
