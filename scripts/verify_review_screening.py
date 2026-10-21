"""Explicit paid check of public source-review cases; no manuscript or tracing."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
FIXTURE = ROOT / 'fixtures/review_counterevidence.json'


async def verify(cases, raw):
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env', override=False)
    os.environ['LANGSMITH_TRACING'] = 'false'
    if not os.getenv('OPENAI_API_KEY'):
        raise SystemExit('OPENAI_API_KEY is not configured. No request made.')
    from backend.provider import OpenAIProvider, call_provider, usage, PROMPT_VERSION
    from backend.documents import document, review

    output = ROOT / 'runtime' / ('screening-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ'))
    output.mkdir(parents=True, exist_ok=False)
    report = {'prompt_version': PROMPT_VERSION, 'fixture_sha256': hashlib.sha256(raw).hexdigest(),
              'trace': False, 'cases': []}
    provider = OpenAIProvider()
    for case in cases:
        doc = document({'title': case['id'], 'passages': [
            {'id': f'p{i+1}', 'text': text} for i, text in enumerate(case['passages'])
        ], 'entities': [], 'relations': []}, 'input', None)
        stats = usage()
        try:
            result = await call_provider(provider, 'reviewer', 'review_source', {
                'mode': 'review', 'request': '원문의 모순과 필요한 설명 부족을 검토해줘.',
                'document': doc, 'context': {'version': 'public-counterevidence', 'documents': [doc],
                                            'constraints': [], 'unresolved_issues': []},
            }, stats, False)
            checked = review(result, doc, [doc])
            actual = [issue['kind'] for issue in checked['issues']]
            expected = [] if case['expected_kind'] is None else [case['expected_kind']]
            entry = {'id': case['id'], 'expected': expected, 'actual': actual,
                     'passed': actual == expected, 'review': checked, 'usage': stats}
        except Exception as exc:
            entry = {'id': case['id'], 'passed': False, 'error_type': type(exc).__name__, 'usage': stats}
        report['cases'].append(entry)
        (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(f"{case['id']}: {'PASS' if entry['passed'] else 'FAIL'}", flush=True)
        if 'error_type' in entry:
            break
    print(output / 'report.json')
    return len(report['cases']) == len(cases) and all(c['passed'] for c in report['cases'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true', help='Run one reviewer call per selected case, each with at most one transient retry')
    parser.add_argument('--case', action='append', dest='case_ids', help='Only run this case ID; repeat to select multiple cases')
    args = parser.parse_args()
    raw = FIXTURE.read_bytes()
    cases = json.loads(raw)
    if args.case_ids:
        unknown = set(args.case_ids) - {case['id'] for case in cases}
        if unknown:
            parser.error('Unknown case IDs: ' + ', '.join(sorted(unknown)))
        cases = [case for case in cases if case['id'] in args.case_ids]
    if not args.execute:
        print(f'{len(cases)} public cases ready; no API calls. Add --execute to run.')
        return
    if not asyncio.run(verify(cases, raw)):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
