"""C-19-11 part 1 : turn the offline test result into the CI job's pass or fail.

  python tools/ci_check.py RESULT.json

RESULT.json is what tools/run_offline_tests.py wrote. The job fails (exit 1) when the file is missing, a test failed or
errored, a baseline test is missing, a module ran out of time, or any test tried the network (network_attempts must be
[]). Prints the counts and, for a failure, the test ids only (no test output : a value could be in it).
"""
import json
from pathlib import Path
import sys


def problems(result):
    total = result.get('total') or {}
    found = []
    if not total.get('executed'):
        found.append('no test ran')
    for key in ('failures', 'errors'):
        if total.get(key):
            found.append(f'{key}: {total[key]}')
    for key in ('network_attempts', 'timed_out_modules', 'missing_baseline_ids'):
        if result.get(key) is None:
            found.append(f'{key} is not in the result')
        elif result[key]:
            found.append(f'{key}: {len(result[key])}')
    return found


def test_ids(result):
    ids = []
    for key in ('failures', 'errors'):
        for item in result.get(key) or []:
            ids.append(item.get('test') if isinstance(item, dict) else None)
    return [i for i in ids if i]


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print(__doc__.strip().splitlines()[2].strip())
        return 2
    path = Path(args[0])
    try:
        result = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        print('offline test result missing or unreadable (the test run did not finish)')
        return 1
    print(json.dumps({'total': result.get('total'), 'groups': result.get('groups'),
                      'network_attempts': len(result.get('network_attempts') or []),
                      'timed_out_modules': result.get('timed_out_modules')}, ensure_ascii=False))
    found = problems(result)
    for line in found:
        print('problem :', line)
    for test in test_ids(result)[:50]:
        print('  ', test)
    return 1 if found else 0


if __name__ == '__main__':
    sys.exit(main())
