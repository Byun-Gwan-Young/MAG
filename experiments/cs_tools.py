"""Local tools of the customer-support task (DEMO-CS, C-17-6 part 1): lookup_order and read_policy.

DEMO-CS v3 (DM-118) adds two process tools, issue_damage_report (shipping) and register_refund (refund). They write
nothing either: what was issued or registered is read back from the session's own earlier calls (context["history"],
the dispatch records), and the order between them comes from process.json in inputs_dir, which agents cannot read
(read_policy reads policies/ only). process.json : {"damage_report_after_refund": true|false}. A refund always needs a
damage report first; with damage_report_after_refund the report also needs the refund first, so the two wait on each
other (the v3 stall variant). The policy documents state the same order in words.

Both only read the scenario's inputs_dir (demo-runner-extension.md 5); nothing is written and nothing outside it is
read. File formats (decision 018; C-18 writes the real CS inputs in them):
- orders.json : a JSON object {"orders": [...]}; every order is a JSON object whose "order_id" is a non-empty string,
  unique in the file. The other fields are free JSON and are returned as they are.
- policies/<name>.md : UTF-8 text. The document name is the file stem: lowercase ASCII letters, digits, '_' or '-',
  starting with a letter or digit. There is no listing call (the design has none); an unknown name gets an error
  result that states the document names.
"""
import hashlib
import json
from pathlib import Path
import re

from experiments.tool_results import Result, ToolFactError

TOOLS = ('lookup_order', 'read_policy', 'issue_damage_report', 'register_refund')
PROCESS_FILE = 'process.json'
PROCESS_KEYS = {'damage_report_after_refund'}
ORDERS_FILE = 'orders.json'
POLICY_DIR = 'policies'
POLICY_SUFFIX = '.md'
POLICY_NAME = re.compile(r'^[a-z0-9][a-z0-9_-]{0,63}$')
# How read_policy may name a document: "refund", "refund.md", "policies/refund.md".
NAME_FORMS = (('', ''), ('', POLICY_SUFFIX), (POLICY_DIR + '/', POLICY_SUFFIX))


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def load_orders(inputs_dir):
    """(orders, sha256 of orders.json). ValueError when the file breaks the format."""
    path = Path(inputs_dir) / ORDERS_FILE
    data = path.read_bytes()
    try:
        value = json.loads(data.decode('utf-8'))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError(ORDERS_FILE + ' is not UTF-8 JSON: ' + type(exc).__name__) from exc
    if not isinstance(value, dict) or set(value) != {'orders'} or not isinstance(value['orders'], list):
        raise ValueError(ORDERS_FILE + ' must be an object with the one key "orders" holding a list')
    ids = []
    for order in value['orders']:
        if not isinstance(order, dict) or not isinstance(order.get('order_id'), str) or not order['order_id'].strip():
            raise ValueError(ORDERS_FILE + ': every order is an object with a non-empty string order_id')
        ids.append(order['order_id'])
    if len(ids) != len(set(ids)):
        raise ValueError(ORDERS_FILE + ': order_id values must be unique')
    return value['orders'], sha256(data)


def policy_documents(inputs_dir):
    """{name: path} of the policy documents. A file that resolves outside inputs_dir (a link) is not a document."""
    root = Path(inputs_dir).resolve()
    folder = Path(inputs_dir) / POLICY_DIR
    if not folder.is_dir():
        return {}
    return {p.name[:-len(POLICY_SUFFIX)]: p for p in sorted(folder.iterdir())
            if p.name.endswith(POLICY_SUFFIX) and POLICY_NAME.match(p.name[:-len(POLICY_SUFFIX)])
            and p.is_file() and p.resolve().is_relative_to(root)}


def input_problems(tool, inputs_dir):
    """What keeps the tool from working on these inputs; checked before a run (C-17-6 part 3). Empty when fine."""
    inputs_dir = Path(inputs_dir)
    if not inputs_dir.is_dir():
        return ['inputs_dir not found']
    if tool == 'lookup_order':
        if not (inputs_dir / ORDERS_FILE).is_file():
            return [ORDERS_FILE + ' not found']
        try:
            load_orders(inputs_dir)
        except (OSError, ValueError) as exc:
            return [str(exc)]
        return []
    if tool == 'read_policy':
        folder = inputs_dir / POLICY_DIR
        if not folder.is_dir():
            return [POLICY_DIR + '/ not found']
        problems = [POLICY_DIR + '/' + p.name + ' is not a policy document (name or location)'
                    for p in sorted(folder.iterdir()) if p.name.endswith(POLICY_SUFFIX)
                    and p.name[:-len(POLICY_SUFFIX)] not in policy_documents(inputs_dir)]
        return problems or ([] if policy_documents(inputs_dir) else [POLICY_DIR + '/ has no policy document'])
    if tool in ('issue_damage_report', 'register_refund'):
        problems = [] if (inputs_dir / ORDERS_FILE).is_file() else [ORDERS_FILE + ' not found']
        try:
            load_process(inputs_dir)
        except (OSError, ValueError) as exc:
            problems.append(str(exc))
        return problems
    raise ValueError('not a local CS tool: ' + tool)


def lookup_order(arguments, context):
    order_id = arguments.get('order_id')
    if not isinstance(order_id, str) or not order_id.strip():
        raise ToolFactError('lookup_order needs order_id as a non-empty string.')
    orders, digest = load_orders(context['inputs_dir'])
    source = {'source': {'file': ORDERS_FILE, 'sha256': digest}}
    for order in orders:
        if order['order_id'] == order_id:
            return Result(order, source)
    raise ToolFactError('No order with order_id ' + json.dumps(order_id, ensure_ascii=False) + ' in the order data.',
                        source)


def requested_name(name):
    """The document name asked for: "refund", "refund.md" or "policies/refund.md". Anything else (other paths,
    "..", absolute paths) names no document."""
    if not isinstance(name, str):
        return None
    for head, tail in NAME_FORMS:
        if name.startswith(head) and name.endswith(tail) and len(name) > len(head) + len(tail):
            stem = name[len(head):len(name) - len(tail)]
            if POLICY_NAME.match(stem):
                return stem
    return None


def read_policy(arguments, context):
    name = arguments.get('name')
    documents = policy_documents(context['inputs_dir'])
    stem = requested_name(name)
    if stem not in documents:
        raise ToolFactError('No policy document named ' + json.dumps(name, ensure_ascii=False) + '. '
                            'read_policy reads only the policy documents of the task: '
                            + (', '.join(documents) if documents else 'none') + '.')
    data = documents[stem].read_bytes()
    return Result(data.decode('utf-8'), {'source': {'file': POLICY_DIR + '/' + documents[stem].name,
                                                    'sha256': sha256(data)}})


def load_process(inputs_dir):
    path = Path(inputs_dir) / PROCESS_FILE
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError as exc:
        raise ValueError(PROCESS_FILE + ' not found') from exc
    if (not isinstance(value, dict) or set(value) != PROCESS_KEYS
            or not isinstance(value['damage_report_after_refund'], bool)):
        raise ValueError(PROCESS_FILE + ' must be {"damage_report_after_refund": true|false}')
    return value


def _order(order_id, inputs_dir):
    if not isinstance(order_id, str) or not order_id.strip():
        raise ToolFactError('order_id must be a non-empty string.')
    orders, _ = load_orders(inputs_dir)
    for order in orders:
        if order['order_id'] == order_id:
            return order
    raise ToolFactError('No order with order_id ' + json.dumps(order_id, ensure_ascii=False) + ' in the order data.')


def _issued(context, tool, order_id):
    """The JSON results of this session's earlier successful calls of tool for order_id."""
    out = []
    for record in context.get('history') or ():
        if record.get('tool') == tool and not record.get('is_error') and record.get('result'):
            try:
                value = json.loads(record['result'])
            except ValueError:
                continue
            if isinstance(value, dict) and value.get('order_id') == order_id:
                out.append(value)
    return out


def issue_damage_report(arguments, context):
    order_id = arguments.get('order_id')
    order = _order(order_id, context['inputs_dir'])
    note = str((order.get('shipping') or {}).get('delivery_note') or '')
    if '파손' not in note:
        raise ToolFactError('배송 메모에 파손 기록이 없어 파손 확인서를 발급할 수 없다.')
    done = _issued(context, 'issue_damage_report', order_id)
    if done:
        return done[0]
    if load_process(context['inputs_dir'])['damage_report_after_refund'] and not _issued(context, 'register_refund',
                                                                                          order_id):
        raise ToolFactError('발급 거절: 파손 확인서는 환불 담당이 이 주문의 환불 등록을 마친 뒤에만 발급한다. '
                            '이 주문의 환불 등록 기록이 없다.')
    return {'damage_report_id': 'DR-' + order_id, 'order_id': order_id, 'status': 'issued'}


def register_refund(arguments, context):
    order_id, amount = arguments.get('order_id'), arguments.get('amount_krw')
    order = _order(order_id, context['inputs_dir'])
    if type(amount) is not int or amount <= 0 or amount > int(order.get('total_krw') or 0):
        raise ToolFactError('amount_krw must be a positive integer not above the order total.')
    done = _issued(context, 'register_refund', order_id)
    if done:
        return done[0]
    if not _issued(context, 'issue_damage_report', order_id):
        raise ToolFactError('등록 거절: 배송 사고 환불은 배송 담당이 발급한 이 주문의 파손 확인서가 있어야 등록할 수 있다. '
                            '이 주문의 파손 확인서가 없다.')
    return {'refund_id': 'RF-' + order_id, 'order_id': order_id, 'amount_krw': amount, 'status': 'registered'}
