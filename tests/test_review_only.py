"""Offline review-only contracts, including real stdio MCP evidence lookup."""
import asyncio
import copy
import json
import re
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.documents import ref
from backend.main import create_app, fingerprint
from backend.provider import MockProvider, OpenAIProvider, call_provider, usage


TEXT = "폭풍 중에는 송신할 수 없다.\n\n리나는 폭풍이 계속되는 동안 송신했다."


class SourceReviewer(MockProvider):
    def __init__(self, case='internal'):
        self.case = case
        self.calls = []

    async def call(self, role, stage, payload):
        self.calls.append((role, stage, copy.deepcopy(payload)))
        if role == 'writer':
            raise AssertionError('review-only must not call writer')
        if role == 'analyst':
            result, tokens = await super().call(role, stage, payload)
            result['entities'] = [dict(id='rule1', type='rule', name='송신 규칙', description='폭풍 중 송신 금지', basis='explicit',
                                       evidence=[dict(document_ref='self', passage_id='p001')])]
            return result, tokens
        assert stage == 'review_source'
        if self.case == 'none':
            return {'issues': []}, {'input_tokens': 2, 'output_tokens': 1}
        target = ref(payload['document'])
        evidence = [dict(document_ref=target, passage_id='p001'), dict(document_ref=target, passage_id='p002')]
        kind = 'internal_conflict'
        if self.case == 'constraint':
            condition = next(d for d in payload['context']['documents'] if d.get('origin') == 'constraint')
            evidence = [dict(document_ref=target, passage_id='p002'), dict(document_ref=ref(condition), passage_id='c1')]
            kind = 'conflict_candidate'
        elif self.case == 'single_paragraph':
            evidence = [dict(document_ref=target, passage_id='p001')]
        elif self.case == 'condition_only':
            condition = next(d for d in payload['context']['documents'] if d.get('origin') == 'constraint')
            evidence = [dict(document_ref=ref(condition), passage_id='c1')]
            kind = 'missing_explanation'
        elif self.case == 'missing_reference':
            evidence[1]['document_ref'] = 'invented:1'
        elif self.case == 'missing_passage':
            evidence[1]['passage_id'] = 'invented'
        elif self.case == 'repeated_passage':
            evidence[1] = dict(evidence[0])
        elif self.case == 'source_is_not_other_baseline':
            kind = 'conflict_candidate'
        issue = dict(id='issue1', kind=kind, reason='송신 금지 규칙과 실제 송신 시점이 충돌합니다.',
                     suggestion='송신 시점이나 예외 조건을 해당 문단에서 설명해주세요.', evidence=evidence, status='open')
        return {'issues': [issue]}, {'input_tokens': 2, 'output_tokens': 1}


def prepare(client, text=TEXT):
    source = client.post('/api/sources', json={'title': '공개 가상 원고', 'text': text}).json()
    response = client.post(f'/api/sources/{source["id"]}/analyze', json={'request_id': 'analyze-'+source['id']})
    assert response.status_code == 200, response.text
    return source, response.json()


def finish(client, payload):
    response = client.post('/api/runs', json=payload)
    assert response.status_code == 202, response.text
    for _ in range(200):
        run = client.get('/api/runs/'+response.json()['id']).json()
        if run['status'] != 'running':
            return run
        time.sleep(.02)
    pytest.fail('review run failed to terminate')


def test_review_original_snapshot_no_writer_and_real_mcp(tmp_path):
    provider = SourceReviewer()
    app = create_app(str(tmp_path/'review.db'), provider)
    with TestClient(app) as client:
        source, analysis = prepare(client)
        run = finish(client, dict(request_id='review', analysis_id=analysis['id'], mode='review', request='모순 위치와 관련 규칙만 알려줘'))
        assert run['status'] == 'needs_attention'
        assert run['mode'] == 'review'
        assert run['review_document'] == analysis['document']
        assert run['draft'] is run['revision'] is None
        assert len(run['reviews']) == 1
        assert run['reviews'][0]['document_ref'] == ref(analysis['document'])
        assert run['reviews'][0]['issues'][0]['kind'] == 'internal_conflict'
        assert [stage for role, stage, _ in provider.calls] == ['analyze', 'review_source']
        assert run['usage']['attempts'] == 2
        assert run['usage']['input_tokens'] == 2
        assert run['evidence_lookup'] == dict(transport='mcp-stdio', tool='get_passages', passage_count=2)
        assert client.get('/api/sources/'+source['id']).json()['text'] == TEXT
        assert client.get('/api/sources/'+source['id']).json()['document'] == analysis['document']
        review_payload = provider.calls[-1][2]
        assert review_payload['request'] == run['request']
        assert review_payload['mode'] == 'review'
        # Later source/analysis mutations cannot replace the stored review snapshot.
        mutated = copy.deepcopy(analysis)
        mutated['document']['passages'][0]['text'] = '다른 분석으로 변경된 자료'
        app.state.store.put('analysis', analysis['id'], mutated)
        assert client.get('/api/runs/'+run['id']).json()['review_document'] == analysis['document']


def test_review_condition_conflict_has_real_source_and_condition_evidence(tmp_path):
    provider = SourceReviewer('constraint')
    with TestClient(create_app(str(tmp_path/'condition.db'), provider)) as client:
        _, analysis = prepare(client)
        run = finish(client, dict(request_id='condition', analysis_id=analysis['id'], mode='review', constraints=['폭풍 중 송신 금지']))
        assert run['status'] == 'needs_attention'
        assert run['draft'] is run['revision'] is None
        issue = run['reviews'][0]['issues'][0]
        assert issue['kind'] == 'conflict_candidate'
        assert len({e['document_ref'] for e in issue['evidence']}) == 2
        assert provider.calls[-1][2]['context']['constraints'] == ['폭풍 중 송신 금지']
        assert run['evidence_lookup']['passage_count'] == 3
        report = client.get(f'/api/runs/{run["id"]}/export')
        assert report.status_code == 200
        assert '송신 금지 규칙과 실제 송신 시점이 충돌' in report.text
        assert '송신 시점이나 예외 조건' in report.text
        assert '#passage-p002' in report.text and '#evidence-1' in report.text
        assert '## evidence-1' in report.text and '폭풍 중 송신 금지' in report.text


@pytest.mark.parametrize('case', ['missing_reference', 'missing_passage', 'repeated_passage', 'source_is_not_other_baseline'])
def test_review_invalid_evidence_fails_without_synthesizing_text(tmp_path, case):
    provider = SourceReviewer(case)
    with TestClient(create_app(str(tmp_path/'invalid.db'), provider)) as client:
        source, analysis = prepare(client)
        run = finish(client, dict(request_id='invalid', analysis_id=analysis['id'], mode='review'))
        assert run['status'] == 'failed'
        assert run['reviews'] == []
        assert run['draft'] is run['revision'] is None
        assert run['review_document'] == analysis['document']
        assert run['usage']['attempts'] == 2
        assert client.get('/api/sources/'+source['id']).json()['text'] == TEXT
        assert all(role != 'writer' for role, _, _ in provider.calls)


def test_review_no_issues_duplicate_mode_alias_and_decision_guards(tmp_path):
    provider = SourceReviewer('none')
    with TestClient(create_app(str(tmp_path/'empty.db'), provider)) as client:
        _, analysis = prepare(client)
        payload = dict(request_id='review-empty', analysis_id=analysis['id'], mode='review')
        run = finish(client, payload)
        assert run['status'] == 'completed'
        assert run['reviews'][0]['issues'] == []
        count = len(provider.calls)
        assert client.post('/api/runs', json=payload).json()['id'] == run['id']
        alias = {**payload, 'request_id': 'review-alias'}
        assert client.post('/api/runs', json=alias).json()['id'] == run['id']
        assert client.post('/api/runs', json={**alias, 'mode': 'create'}).status_code == 409
        assert client.post('/api/runs', json={**payload, 'mode': 'create', 'request_id': 'new-mode'}).status_code == 409
        for status in ('accepted', 'deferred'):
            rejected = client.post(f'/api/runs/{run["id"]}/decision', json=dict(status=status, reason='검토'))
            assert rejected.status_code == 409 and '생성 문서가 없습니다' in rejected.json()['detail']
        rejected = client.post('/api/runs', json=dict(request_id='next', accepted_run_id=run['id']))
        assert rejected.status_code == 409 and '원문 검토' in rejected.json()['detail']
        assert client.post('/api/runs', json=dict(request_id='wrong-origin', mode='review', accepted_run_id=run['id'])).status_code == 422
        assert client.get('/api/runs').json()['runs'][0]['mode'] == 'review'
        assert len(provider.calls) == count
        report = client.get(f'/api/runs/{run["id"]}/export').text
        assert '원문 검토 보고서' in report
        assert '근거를 갖춘 쟁점을 찾지 못했습니다' in report
        assert '모의] 폭풍 관측소' not in report


def test_review_graph_export_sse_and_reload_keep_original_refs(tmp_path):
    path = str(tmp_path/'reload.db')
    provider = SourceReviewer()
    with TestClient(create_app(path, provider)) as client:
        _, analysis = prepare(client)
        run = finish(client, dict(request_id='reload', analysis_id=analysis['id'], mode='review'))
        graphs = [client.get(f'/api/runs/{run["id"]}/graph'+query) for query in ('', '?view=draft', '?view=input')]
        assert all(response.status_code == 200 for response in graphs)
        assert graphs[0].json() == graphs[1].json() == graphs[2].json()
        nodes = graphs[0].json()['nodes']
        assert len(nodes) == 1  # Same source is both context and target, only once.
        assert nodes[0]['data']['document_ref'] == ref(analysis['document'])
        assert nodes[0]['data']['kind'] == 'input'
        assert nodes[0]['data']['issue_ids'] == ['issue1']
        assert client.get(f'/api/runs/{run["id"]}/graph?view=revision').status_code == 409
        report = client.get(f'/api/runs/{run["id"]}/export').text
        anchors = set(re.findall(r'^## ([A-Za-z0-9_-]+)$', report, re.M))
        assert all(link in anchors for link in re.findall(r'\]\(#([^)]*)\)', report))
        for passage in analysis['document']['passages']:
            assert passage['text'] in report
        stream = client.get(f'/api/runs/{run["id"]}/events').text
        assert 'review_source' in stream and 'needs_attention' in stream
        assert '"stage": "generate"' not in stream and '"stage": "revise"' not in stream
        last = re.findall(r'^id: (\d+)$', stream, re.M)[-1]
        assert client.get(f'/api/runs/{run["id"]}/events', headers={'Last-Event-ID': last}).text == ''
    fresh_provider = SourceReviewer()
    with TestClient(create_app(path, fresh_provider)) as client:
        restored = client.get('/api/runs/'+run['id']).json()
        assert restored == run
        assert client.get('/api/runs').json()['runs'][0]['mode'] == 'review'
        assert client.get(f'/api/runs/{run["id"]}/export').text == report
        assert fresh_provider.calls == []


def test_old_run_mode_and_old_request_fingerprint_remain_compatible(tmp_path):
    app = create_app(str(tmp_path/'old.db'), MockProvider())
    old = dict(id='old-run', status='completed', stage='finished')
    app.state.store.put('run', old['id'], old)
    old_payload = dict(request_id='legacy-request', analysis_id='old-analysis', accepted_run_id=None, constraints=[], request='', trace=False)
    app.state.store.claim('legacy-request', 'run', old['id'], fingerprint(old_payload))
    with TestClient(app) as client:
        assert client.get('/api/runs/old-run').json()['mode'] == 'create'
        assert client.get('/api/runs').json()['runs'][0]['mode'] == 'create'
        assert client.post('/api/runs', json=dict(request_id='legacy-request', analysis_id='old-analysis')).json()['id'] == old['id']
        assert client.post('/api/runs', json=dict(request_id='legacy-request', analysis_id='old-analysis', mode='review')).status_code == 409


@pytest.mark.asyncio
async def test_actual_openai_review_source_prompt_and_payload_offline(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'offline-test-key')
    captured = []
    class Responses:
        async def create(self, **kwargs):
            captured.append(kwargs)
            return SimpleNamespace(status='completed', output_text='{"issues":[]}', model='offline-model', usage=SimpleNamespace(input_tokens=1, output_tokens=1))
    provider = OpenAIProvider()
    provider.client = SimpleNamespace(responses=Responses())
    document = dict(document_id='source-test', version=1, passages=[dict(id='p001', text='공개 가상 원문')], entities=[])
    payload = dict(mode='review', document=document, context=dict(documents=[document], constraints=['보호 규칙']), request='문제 위치만 검토')
    await call_provider(provider, 'reviewer', 'review_source', payload, usage())
    body = json.loads(captured[0]['input'][1]['content'])
    assert body['stage'] == 'review_source' and body['mode'] == 'review'
    assert body['request'] == payload['request']
    assert body['target_document_ref'] == 'source-test:1'
    assert len(body['reference_index']) == 1
    system = captured[0]['input'][0]['content']
    assert '불변 원문' in system and 'issues를 빈 목록' in system
    assert 'internal_conflict' in system and 'origin=constraint' in system
    assert '원문을 재작성하거나 새로운 이야기' in system
    assert '그 문단 근거를 한 번만 인용' in system
    assert '서로 충돌하는 두 주장을 구체적으로 비교' in system
    assert '내부 모순은 대상의 서로 다른 두 문단 근거가 필요하다' not in system


@pytest.mark.parametrize('text, expected', [(TEXT, 'needs_attention'), ('청동 열쇠는 세상에 하나뿐이다. 정오에 지민은 그 열쇠를 혼자 주머니에 넣은 채 탑 꼭대기에 있었다.\n\n같은 날 정오에 수아는 같은 청동 열쇠를 혼자 주머니에 넣은 채 지하 창고에 있었다. 탑과 창고 사이에는 이동이나 복제 장치가 없다.', 'needs_attention'), ('리나는 맑은 날 송신했다.\n\n폭풍이 멎었다.', 'completed')])
def test_mock_review_uses_only_explicit_public_pair(tmp_path, text, expected):
    with TestClient(create_app(str(tmp_path/'mock.db'), MockProvider())) as client:
        _, analysis = prepare(client, text)
        run = finish(client, dict(request_id='mock', analysis_id=analysis['id'], mode='review'))
        assert run['status'] == expected
        assert run['review_document'] == analysis['document']
        assert run['draft'] is run['revision'] is None
        assert all(call['role'] != 'writer' for call in run['usage']['calls'])
        if run['reviews'][0]['issues']:
            assert {e['passage_id'] for e in run['reviews'][0]['issues'][0]['evidence']} == {'p001', 'p002'}


def test_review_retry_budget_shared_with_analysis(tmp_path):
    class Transient(Exception):
        status_code = 503
    class RetriedReviewer(SourceReviewer):
        failed_once = False
        async def call(self, role, stage, payload):
            if role == 'reviewer' and not self.failed_once:
                self.failed_once = True
                raise Transient()
            return await super().call(role, stage, payload)
    with TestClient(create_app(str(tmp_path/'retry.db'), RetriedReviewer('none'))) as client:
        _, analysis = prepare(client)
        run = finish(client, dict(request_id='retry', analysis_id=analysis['id'], mode='review'))
        assert run['status'] == 'completed'
        assert run['usage']['attempts'] == 3 and run['usage']['retries'] == 1
        assert [c['role'] for c in run['usage']['calls']] == ['analyst', 'reviewer', 'reviewer']
        assert [c['status'] for c in run['usage']['calls']] == ['completed', 'failed', 'completed']


def test_review_restart_marks_interrupted_without_resuming(tmp_path):
    path = str(tmp_path/'interrupted.db')
    app = create_app(path, SourceReviewer())
    app.state.store.put('run', 'interrupted-review', dict(id='interrupted-review', mode='review', status='running', stage='review_source', review_document={'unchanged': True}, draft=None, revision=None))
    provider = SourceReviewer()
    with TestClient(create_app(path, provider)) as client:
        run = client.get('/api/runs/interrupted-review').json()
        assert run['mode'] == 'review' and run['status'] == 'interrupted'
        assert run['review_document'] == {'unchanged': True}
        assert run['draft'] is run['revision'] is None
        assert 'interrupted' in client.get('/api/runs/interrupted-review/events').text
        assert provider.calls == []


def test_review_mcp_mismatch_prevents_reviewer_and_releases_gate(tmp_path, monkeypatch):
    import backend.workflow as workflow
    original = workflow.get_passages
    async def wrong(*args):
        return []
    provider = SourceReviewer('none')
    with TestClient(create_app(str(tmp_path/'mismatch.db'), provider)) as client:
        _, analysis = prepare(client)
        monkeypatch.setattr(workflow, 'get_passages', wrong)
        run = finish(client, dict(request_id='bad-mcp', analysis_id=analysis['id'], mode='review'))
        assert run['status'] == 'failed' and 'MCP' in run['error']
        assert [role for role, _, _ in provider.calls] == ['analyst']
        assert run['draft'] is run['revision'] is None
        monkeypatch.setattr(workflow, 'get_passages', original)
        # A subsequent analysis can acquire the same gate after a failed review.
        _, second_analysis = prepare(client)
        completed = finish(client, dict(request_id='after-failure', analysis_id=second_analysis['id'], mode='review'))
        assert completed['status'] == 'completed'


def test_review_issue_requires_a_location_in_the_original_source(tmp_path):
    provider = SourceReviewer('condition_only')
    with TestClient(create_app(str(tmp_path/'condition-only.db'), provider)) as client:
        _, analysis = prepare(client)
        run = finish(client, dict(request_id='condition-only', analysis_id=analysis['id'], mode='review', constraints=['송신 금지']))
        assert run['status'] == 'failed'
        assert '대상 원문의 문단 근거' in run['error']
        assert run['reviews'] == [] and run['draft'] is run['revision'] is None


def test_review_internal_conflict_within_one_original_paragraph(tmp_path):
    text = '폭풍 중에는 송신할 수 없다. 리나는 폭풍이 계속되는 동안 송신했다.'
    provider = SourceReviewer('single_paragraph')
    with TestClient(create_app(str(tmp_path/'one-paragraph.db'), provider)) as client:
        source, analysis = prepare(client, text)
        assert len(analysis['document']['passages']) == 1
        run = finish(client, dict(request_id='one-paragraph', analysis_id=analysis['id'], mode='review'))
        assert run['status'] == 'needs_attention'
        assert run['draft'] is run['revision'] is None
        assert run['review_document'] == analysis['document']
        assert run['review_document']['passages'] == [dict(id='p001', text=text)]
        issue = run['reviews'][0]['issues'][0]
        assert issue['kind'] == 'internal_conflict'
        assert issue['evidence'] == [dict(document_ref=ref(analysis['document']), passage_id='p001')]
        assert '송신 금지 규칙과 실제 송신 시점' in issue['reason']
        assert run['evidence_lookup']['passage_count'] == 1
        assert client.get('/api/sources/'+source['id']).json()['text'] == text
        assert [role for role, _, _ in provider.calls] == ['analyst', 'reviewer']
        exported = client.get(f'/api/runs/{run["id"]}/export')
        assert exported.status_code == 200
        assert text in exported.text and '#passage-p001' in exported.text
