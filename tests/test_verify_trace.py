"""Offline checks that trace verification cannot omit a failed or missing root."""
import copy
import json
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from scripts import verify_trace


def case():
    call = {'stage': 'analyze', 'role': 'analyst', 'model': 'offline-model', 'input_tokens': 2, 'output_tokens': 3}
    analysis = {'id': 'analysis-one', 'source_id': 'source-one', 'status': 'completed', 'usage': {'trace_id': 'analysis-trace', 'calls': [call]}}
    generated = {**call, 'stage': 'generate', 'role': 'writer'}
    run = {'id': 'run-one', 'source_id': 'source-one', 'analysis_id': 'analysis-one', 'accepted_run_id': None, 'status': 'completed', 'trace': True, 'draft': {}, 'warnings': [], 'usage': {'trace_id': 'job-trace', 'calls': [call, generated]}}
    return {'trace_requested': True, 'runs': [copy.deepcopy(run)]}, [('analysis', analysis), ('run', run)]


def test_expected_source_analysis_and_report_run_are_both_required():
    report, objects = case()
    roots, errors = verify_trace.collect_roots(report, objects)
    assert not errors
    assert set(roots) == {'analysis-trace', 'job-trace'}
    assert [c['stage'] for c in roots['job-trace']['calls']] == ['generate']


@pytest.mark.parametrize('fault', ['missing_job_trace', 'missing_analysis_trace', 'missing_job_record', 'missing_analysis_record', 'trace_warnings', 'root_warning', 'report_warning', 'untraced_report', 'untraced_job', 'failed_job', 'analysis_failed', 'unreported_job', 'unreported_analysis', 'wrong_source', 'wrong_report_trace', 'wrong_report_calls'])
def test_incomplete_or_failed_trace_coverage_is_never_a_pass(fault):
    report, objects = case()
    if fault == 'missing_job_trace': objects[1][1]['usage'].pop('trace_id')
    elif fault == 'missing_analysis_trace': objects[0][1]['usage'].pop('trace_id')
    elif fault == 'missing_job_record': objects.pop(1)
    elif fault == 'missing_analysis_record': objects.pop(0)
    elif fault == 'trace_warnings': objects[1][1]['usage']['trace_warnings'] = ['LangSmith metadata upload failed']
    elif fault == 'root_warning': objects[1][1]['warnings'] = ['LangSmith 추적 미설정 또는 연결 실패']
    elif fault == 'report_warning': report['runs'][0]['warnings'] = ['LangSmith 추적 종료 전송 실패']
    elif fault == 'untraced_report': report['trace_requested'] = False
    elif fault == 'untraced_job': objects[1][1]['trace'] = False
    elif fault == 'failed_job': objects[1][1]['status'] = report['runs'][0]['status'] = 'failed'
    elif fault == 'analysis_failed':
        report['runs'] = [{'case': 'keywords', 'status': 'analysis_failed'}]
        objects = [('analysis', objects[0][1])]
        objects[0][1]['status'] = 'failed'
    elif fault == 'unreported_job':
        extra = copy.deepcopy(objects[1][1]); extra['id'] = 'other-job'; extra['usage']['trace_id'] = 'other-trace'; objects.append(('run', extra))
    elif fault == 'unreported_analysis':
        extra = copy.deepcopy(objects[0][1]); extra['id'] = 'other-analysis'; extra['source_id'] = 'other-source'; extra['usage']['trace_id'] = 'other-trace'; objects.append(('analysis', extra))
    elif fault == 'wrong_source': objects[0][1]['source_id'] = 'different-source'
    elif fault == 'wrong_report_trace': report['runs'][0]['usage']['trace_id'] = 'different-trace'
    elif fault == 'wrong_report_calls': report['runs'][0]['usage']['calls'] = []
    _, errors = verify_trace.collect_roots(report, objects)
    assert errors, fault


def test_continuation_and_its_original_source_analysis_are_covered_once():
    report, objects = case()
    continuation = copy.deepcopy(objects[1][1])
    continuation.update(id='continued-run', analysis_id=None, accepted_run_id='run-one')
    continuation['usage'] = {'trace_id': 'continued-trace', 'calls': [{**objects[0][1]['usage']['calls'][0], 'stage': 'analyze_continuation'}]}
    objects.append(('run', continuation))
    report['runs'].append(copy.deepcopy(continuation))
    roots, errors = verify_trace.collect_roots(report, objects)
    assert not errors
    assert len(roots) == 3
    assert roots['continued-trace']['calls'][0]['stage'] == 'analyze_continuation'


def write_case(tmp_path, report, objects):
    path = tmp_path / 'report.json'
    path.write_text(json.dumps(report))
    with sqlite3.connect(tmp_path / 'live.db') as db:
        db.execute('CREATE TABLE objects(kind TEXT, id TEXT, value TEXT)')
        db.executemany('INSERT INTO objects VALUES(?,?,?)', [(kind, item['id'], json.dumps(item)) for kind, item in objects])
    return path


def test_main_persists_coverage_failure_without_constructing_remote_client(tmp_path, monkeypatch):
    report, objects = case()
    objects[1][1]['usage'].pop('trace_id')
    path = write_case(tmp_path, report, objects)
    monkeypatch.setenv('LANGSMITH_API_KEY', 'offline-placeholder')
    def forbidden(**kwargs):
        raise AssertionError('Local coverage failure must not call remote service')
    monkeypatch.setattr('langsmith.Client', forbidden)
    with pytest.raises(SystemExit) as error:
        verify_trace.main(path)
    assert error.value.code == 1
    proof = json.loads((tmp_path / 'trace-verification.json').read_text())
    assert not proof['passed']
    assert proof['coverage_errors']
    assert proof['traces'] == []


def test_main_complete_readback_still_passes_offline(tmp_path, monkeypatch):
    report, objects = case()
    path = write_case(tmp_path, report, objects)
    def span(id, name, parent=None, metadata=None, children=None):
        return SimpleNamespace(id=id, name='LoreForge/' + name, parent_run_id=parent, inputs={}, extra={'metadata': metadata or {}}, outputs={'status': 'completed'}, end_time=datetime.now(timezone.utc), child_runs=children or [])
    remotes = {}
    for kind, item in objects:
        root_id = item['usage']['trace_id']
        calls = item['usage']['calls'] if kind == 'analysis' else item['usage']['calls'][1:]
        children = [span('child-'+c['stage'], c['stage'], root_id, {k: v for k, v in c.items() if k != 'stage'}) for c in calls]
        if kind == 'run': children += [span('mcp', 'mcp/get_passages', root_id), span('branch', 'branch', root_id)]
        remotes[root_id] = span(root_id, 'analysis' if kind == 'analysis' else 'job', children=children)
    class FakeClient:
        def __init__(self, **kwargs): pass
        def read_run(self, id, load_child_runs): return remotes[id]
        def get_run_url(self, run): return 'https://example.invalid/offline/' + run.id
    monkeypatch.setenv('LANGSMITH_API_KEY', 'offline-placeholder')
    monkeypatch.setattr('langsmith.Client', FakeClient)
    verify_trace.main(path)
    proof = json.loads((tmp_path / 'trace-verification.json').read_text())
    assert proof['passed']
    assert proof['coverage_errors'] == []
    assert len(proof['traces']) == 2
