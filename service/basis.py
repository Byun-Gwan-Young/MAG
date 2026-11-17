"""C-19-9b part 2 (DM-120) : one Korean line per step saying why the step was judged as it was.

A screen-only sentence built from signal values that are already recorded (the judgement's state, consecutive count
and STOP sentence ; the step's tests, changed files and structured report). No LLM, no model text, and nothing here
changes a judgement : the frozen judge (deadlock-v0.3) and its STOP sentences (explain-v1) are only read. A STOP step
keeps its recorded sentence as it is.

Comparison is with the same role's previous step (deadlock-v0.3 comparison_scope : same_agent_id). The "n/3" counter
is the recorded consecutive count over the frozen consecutive_steps parameter (docs/criteria/deadlock-v0.3.json).

Two adapters build the common step shape : `from_live` (the live API's step row plus the judgement's progress and
criteria) and `from_replay` (a demo replay.json step with its judgement values). `annotate` walks a session's steps in
order and returns the lines.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CRITERIA_RECORD = ROOT / 'docs' / 'criteria' / 'deadlock-v0.3.json'
STOP_DECISIONS = ('deadlock', 'termination_failure')
METRIC_NAMES = {'artifact.test_pass_count': '통과 테스트', 'artifact.files_changed': '파일 변화',
                'conversation.open_issues_count': '미해결 쟁점', 'conversation.status': '처리 상태',
                'conversation.adopted_items': '채택 항목', 'conversation.open_issue_ids': '미해결 쟁점'}
REASONS = {'agent_id missing': '역할 ID 가 기록되지 않았습니다',
           'metric selection changed': '이전 관측과 지표 구성이 달라 비교할 수 없습니다',
           'unsupported T1 metric': '지원하지 않는 대화 지표가 선택되었습니다',
           'unsupported progress version': '지원하지 않는 진전도 버전입니다',
           'nonconsecutive step IDs': 'step 번호가 이어지지 않습니다'}
MAX_NAMES = 3
PASS_COUNT = 'artifact.test_pass_count'


def threshold(record=CRITERIA_RECORD):
    """The frozen consecutive_steps parameter (N of the "n/N" counter), or None when the record cannot be read."""
    try:
        value = json.loads(Path(record).read_text(encoding='utf-8'))['criteria_parameters']['consecutive_steps']
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _count(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, (list, tuple)):
        return len(value)
    return None


def _ids(value):
    return [str(x) for x in value] if isinstance(value, (list, tuple)) else None


def _names(ids):
    shown = ', '.join(ids[:MAX_NAMES])
    return shown + (f' 외 {len(ids) - MAX_NAMES} 개' if len(ids) > MAX_NAMES else '')


def _tests(value):
    """(passed, total) from [passed, total] or {'passed', 'total'}, else None."""
    if isinstance(value, dict):
        value = [value.get('passed'), value.get('total')]
    if isinstance(value, (list, tuple)) and len(value) == 2 and all(_count(v) is not None for v in value):
        return int(value[0]), int(value[1])
    return None


def step_shape(*, n, agent, state, decision=None, stop_text=None, blocked=False, tests=None, paths=None,
               status=None, adopted=None, issue_ids=None, consecutive=None, revert_to=None, reason=None,
               unmeasured_metrics=(), events=()):
    return {'n': n, 'agent': agent, 'state': state, 'decision': decision, 'stop_text': stop_text,
            'blocked': bool(blocked), 'tests': _tests(tests), 'paths': list(paths) if paths is not None else None,
            'status': status, 'adopted': _count(adopted), 'issue_ids': _ids(issue_ids),
            'consecutive': _count(consecutive), 'revert_to': _count(revert_to), 'reason': reason,
            'unmeasured_metrics': list(unmeasured_metrics or ()), 'events': list(events or ())}


def from_live(row, progress=None, criteria=None):
    """The live API's step row (backend/routers/live._step_row) with the judgement's stored progress and criteria."""
    progress, criteria = progress or {}, criteria or {}
    report = row.get('report') or {}
    t3 = progress.get('t3') or {}
    metrics = progress.get('metrics') or {}
    missing = [k for k, v in metrics.items() if isinstance(v, dict) and v.get('status') == 'unmeasured']
    return step_shape(n=row.get('n'), agent=row.get('agent'), state=row.get('state'),
                      decision=criteria.get('decision'), stop_text=(row.get('stop') or {}).get('text'),
                      blocked=row.get('blocked'), tests=row.get('tests'),
                      paths=row.get('paths') if (row.get('tests') is not None or t3) else None,
                      status=report.get('status'), adopted=report.get('adopted'),
                      issue_ids=report.get('issue_ids') if report else None,
                      consecutive=criteria.get('consecutive_steps'),
                      revert_to=t3.get('revert_to_step') if t3.get('global_revert') else None,
                      reason=progress.get('reason') or (criteria.get('reason') if criteria.get('decision') ==
                                                        'unmeasured' else None),
                      unmeasured_metrics=missing, events=progress.get('events') or ())


def from_replay(fact):
    """A demo replay.json step (tools/replay_demo.replay_rows) with its judgement values (service/queries)."""
    report = fact.get('report') or {}
    judgement = fact.get('judgement') or {}
    return step_shape(n=fact.get('step_id'), agent=fact.get('agent'), state=fact.get('state'),
                      decision=fact.get('decision'), stop_text=(fact.get('stop') or {}).get('text'),
                      blocked=fact.get('blocked'), tests=fact.get('tests'),
                      paths=fact.get('changed_paths') if fact.get('tests') is not None else None,
                      status=report.get('status_label') or report.get('status'),
                      adopted=report.get('adopted_items'), issue_ids=report.get('open_issue_ids') if report else None,
                      consecutive=judgement.get('consecutive_steps'))


def _counter(step, limit):
    n = step['consecutive']
    if n is None:
        return ''
    return f' → 진전 없는 차례 {n}/{limit}' if limit else f' → 진전 없는 차례 {n}'


def _progress_items(step, previous):
    items = []
    if step['paths']:
        items.append(f'새 파일 내용 {len(step["paths"])} 개')
    if step['tests'] and previous and previous['tests'] and step['tests'][0] != previous['tests'][0]:
        items.append(f'통과 테스트 {previous["tests"][0]} → {step["tests"][0]}')
    if step['issue_ids'] is not None and previous and previous['issue_ids'] is not None:
        closed = [i for i in previous['issue_ids'] if i not in step['issue_ids']]
        opened = [i for i in step['issue_ids'] if i not in previous['issue_ids']]
        if closed:
            items.append(f'쟁점 {_names(closed)} 닫힘')
        if opened:
            items.append(f'새 쟁점 {_names(opened)}')
    if step['adopted'] is not None and previous and previous['adopted'] is not None \
            and step['adopted'] != previous['adopted']:
        items.append(f'채택 {previous["adopted"]} → {step["adopted"]}')
    if step['status'] and previous and previous['status'] and step['status'] != previous['status']:
        items.append(f"처리 상태 '{previous['status']}' → '{step['status']}'")
    if not items:
        items = _event_items(step['events'])
    return items


def _event_items(events):
    """The judge's own progress events (progress-v3, same-role comparison), used when the step facts above show no
    difference : e.g. a file differs from this role's previous snapshot but not from the previous global step."""
    files, closed, other = [], [], []
    for event in events:
        if not isinstance(event, dict):
            continue
        metric, kind, item = event.get('metric'), event.get('kind'), event.get('item')
        if metric == 'artifact.files_changed' and item:
            files.append(str(item))
        elif metric == 'conversation.open_issues_count' and kind == 'closed' and item:
            closed.append(str(item))
        elif metric == PASS_COUNT:
            other.append('통과 테스트 최고치 갱신')
        elif metric:
            other.append(f'{METRIC_NAMES.get(metric, metric)} 바뀜')
    items = [f'새 파일 내용 {len(files)} 개 (이 역할의 직전 관측 기준)'] if files else []
    items += [f'쟁점 {_names(closed)} 닫힘'] if closed else []
    return items + sorted(set(other))


def _same_items(step, previous):
    items = []
    if step['paths'] is not None:
        if step['paths']:
            items.append(f'파일 {len(step["paths"])} 개 바뀌었지만')
        else:
            items.append('파일 변화 없음')
    if step['tests']:
        items.append(f'통과 테스트 {step["tests"][0]}/{step["tests"][1]} 그대로')
    if step['issue_ids'] is not None:
        opened = [i for i in step['issue_ids'] if previous and previous['issue_ids'] is not None
                  and i not in previous['issue_ids']]
        items.append(f'새 쟁점 {_names(opened)}' if opened else '새 쟁점 없음')
    if step['tests'] is None and step['status']:
        items.append(f"처리 상태 '{step['status']}' 그대로")
    if step['tests'] is None and step['adopted'] is not None:
        items.append(f'채택 {step["adopted"]} 그대로')
    return items


def line(step, previous=None, limit=None):
    """The basis line for one step (step_shape), given the same role's previous step."""
    if step['stop_text']:
        return step['stop_text']
    if step['blocked']:
        return '차단 : STOP 뒤의 요청이라 업스트림으로 보내지 않고 403 으로 돌려주었습니다'
    state = step['state']
    if state == 'baseline':
        if previous is None:
            return '기준점 : 이 역할의 첫 관측이라 비교할 이전 기록이 없습니다'
        return '기준점 : 비교 기준을 새로 잡았습니다 (이전 관측의 지표가 측정되지 않음)'
    if state == 'declared':
        return '완료 선언 : 이 단계에서 완료를 선언했습니다'
    if state == 'progress':
        items = _progress_items(step, previous)
        return ' · '.join(items) if items else '진전 : 신호가 바뀌었지만 세부 변화는 기록되지 않았습니다'
    if state == 'stagnant':
        items = _same_items(step, previous)
        return (' · '.join(items) if items else '바뀐 신호 없음') + _counter(step, limit)
    if state == 'oscillating':
        items = []
        if step['revert_to'] is not None:
            items.append(f'파일 상태가 {step["revert_to"]}단계 내용으로 돌아감')
        reopened = [e.get('item') for e in step['events'] if isinstance(e, dict)
                    and e.get('kind') in ('reopen', 'oscillation', 'revert') and e.get('item')]
        if reopened:
            items.append(f'{_names([str(x) for x in reopened])} 예전 값으로 돌아감')
        if not items:
            items.append('예전 상태로 돌아감')
        if step['tests']:
            items.append(f'통과 테스트 {step["tests"][0]}/{step["tests"][1]}')
        return '되돌림 : ' + ' · '.join(items) + _counter(step, limit)
    if state == 'unmeasured' or step['decision'] == 'unmeasured':
        if step['reason'] in REASONS:
            return '측정 불가 : ' + REASONS[step['reason']]
        names = sorted({METRIC_NAMES.get(m, m) for m in step['unmeasured_metrics']})
        if names:
            return '측정 불가 : ' + ' · '.join(names) + ' 값을 측정하지 못했습니다'
        return '측정 불가 : 판정에 필요한 관측값이 기록되지 않았습니다'
    return '판정 기록 없음'


def annotate(steps, limit=None):
    """Basis lines for a session's steps (step_shape, in step order), each compared with its role's previous step."""
    limit = threshold() if limit is None else limit
    previous, lines = {}, []
    for step in steps:
        lines.append(line(step, previous.get(step['agent']), limit))
        if not step['blocked']:
            previous[step['agent']] = step
    return lines
