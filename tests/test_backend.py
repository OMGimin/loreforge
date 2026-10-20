from backend.provider import PROMPT_VERSION
import asyncio
import copy
import time
import pytest
from fastapi.testclient import TestClient
from backend.main import create_app
from backend.provider import MockProvider, call_provider, usage
from backend.documents import document, review, ref, graph, export_markdown
from backend.store import Store
from backend.mcp_server import get_passages

@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(str(tmp_path / 'store.db'), MockProvider())) as client:
        yield client


def prepared(client, text="폭풍 관측소\n\n폭풍 중 송신할 수 없다."):
    source = client.post('/api/sources', json={"text": text}).json()
    response = client.post(f'/api/sources/{source["id"]}/analyze', json={"request_id": 'a'+source['id']})
    assert response.status_code == 200
    return source, response.json()


def completed(client, analysis, **extra):
    response = client.post('/api/runs', json={"request_id": 'r'+analysis['id'], "analysis_id": analysis['id'], **extra})
    assert response.status_code == 202, response.text
    id = response.json()['id']
    for _ in range(150):
        run = client.get('/api/runs/'+id).json()
        if run['status'] != 'running':
            assert run['status'] == 'completed', run
            return run
        time.sleep(.03)
    pytest.fail('mock run did not finish')


def test_real_mcp_and_full_revision_loop(client):
    source, analysis = prepared(client)
    run = completed(client, analysis, constraints=['폭풍 중 송신 금지'])
    assert run['usage']['attempts'] == 5
    assert run['prompt_version'] == analysis['prompt_version'] == PROMPT_VERSION
    assert len(run['reviews']) == 2
    assert run['draft']['version'] == 1 and run['revision']['version'] == 2
    assert run['reviews'][0]['document_ref'] != run['reviews'][1]['document_ref']
    assert run['evidence_lookup']['transport'] == 'mcp-stdio'
    assert run['evidence_lookup']['passage_count'] == 3
    assert client.get('/api/sources/'+source['id']).json()['text'] == source['text']
    export = client.get(f'/api/runs/{run["id"]}/export')
    assert export.status_code == 200
    assert '#evidence-' in export.text
    assert 'mock' in export.text
    assert client.get(f'/api/runs/{run["id"]}/graph?view=revision').json()['edges']


def test_idempotency_and_analysis_consumption(client):
    source, analysis = prepared(client)
    again = client.post(f'/api/sources/{source["id"]}/analyze', json={"request_id": 'a'+source['id']}).json()
    assert again['id'] == analysis['id']
    run = completed(client, analysis)
    repeated = client.post('/api/runs', json={"request_id": 'duplicate', "analysis_id": analysis['id']})
    assert repeated.json()['id'] == run['id']
    conflict = client.post('/api/runs', json={"request_id": 'r'+analysis['id'], "analysis_id": analysis['id'], 'request': 'different'})
    assert conflict.status_code == 409
    assert len(client.get('/api/runs').json()['runs']) == 1


def test_decision_snapshot_and_restart(client, tmp_path):
    _, analysis = prepared(client)
    run = completed(client, analysis)
    forbidden = client.post('/api/runs', json={'request_id': 'next', 'accepted_run_id': run['id']})
    assert forbidden.status_code == 409
    accepted = client.post(f'/api/runs/{run["id"]}/decision', json={'status': 'accepted', 'reason': '계속 쓰기'}).json()
    response = client.post('/api/runs', json={'request_id': 'next', 'accepted_run_id': run['id'], 'constraints': ['한 사건만']})
    assert response.status_code == 202
    continuation = response.json()
    refs = [ref(d) for d in continuation['context']['documents']]
    assert ref(accepted['revision']) in refs and ref(accepted['draft']) not in refs
    client.post(f'/api/runs/{run["id"]}/decision', json={'status': 'deferred', 'reason': '생각 변경'})
    snapshot = client.get('/api/runs/'+continuation['id']).json()
    assert next(d for d in snapshot['context']['documents'] if ref(d) == ref(accepted['revision']))['decision'] == 'accepted'


def test_sse_replay_does_not_execute(client):
    _, analysis = prepared(client)
    run = completed(client, analysis)
    response = client.get(f'/api/runs/{run["id"]}/events')
    ids = [line for line in response.text.splitlines() if line.startswith('id:')]
    assert len(ids) >= 5
    last = ids[-1].split(':')[1].strip()
    assert client.get(f'/api/runs/{run["id"]}/events', headers={'Last-Event-ID': last}).text == ''
    assert client.get('/api/runs/'+run['id']).json()['usage']['attempts'] == 5


def body():
    return {'title': '본문', 'passages': [{'id': 'p1', 'text': '리나는 기다렸다.'}, {'id': 'p2', 'text': '다시 기다렸다.'}], 'entities': [], 'relations': []}


def test_graph_invalid_fragments_preserve_body():
    raw = body()
    raw['entities'] = [None, {'id': []}, {'id': 'x', 'type': 'person', 'name': '리나', 'description': '', 'basis': 'explicit', 'evidence': [{'document_ref': 'invented:1', 'passage_id': 'p1'}]}]
    raw['relations'] = [None]
    doc = document(raw, 'draft', 'ctx')
    assert doc['passages'] == raw['passages']
    assert doc['graph_status'] == 'unavailable'
    assert len(doc['warnings']) == 4


def test_duplicate_graph_id_and_missing_endpoint_excluded():
    raw = body()
    entity = {'id': 'e1', 'type': 'person', 'name': '리나', 'description': '', 'basis': 'explicit', 'evidence': [{'document_ref': 'self', 'passage_id': 'p1'}]}
    raw['entities'] = [entity, copy.deepcopy(entity)]
    raw['relations'] = [{'id': 'r1', 'source': 'e1', 'target': 'unknown', 'type': 'uses', 'label': '사용', 'condition': '', 'basis': 'explicit', 'evidence': [{'document_ref': 'self', 'passage_id': 'p1'}]}]
    doc = document(raw, 'draft', 'ctx')
    assert doc['entities'] == [] and doc['relations'] == []
    assert len(doc['warnings']) == 3


def test_review_requires_real_two_sided_evidence():
    doc = document(body(), 'draft', 'ctx')
    issue = {'id': 'i1', 'kind': 'internal_conflict', 'reason': '불일치', 'suggestion': '설명', 'evidence': [{'document_ref': 'self', 'passage_id': 'p1'}], 'status': 'resolved'}
    with pytest.raises(ValueError, match='두 곳'):
        review({'issues': [issue]}, doc, [])
    issue['evidence'].append({'document_ref': 'self', 'passage_id': 'p2'})
    assert review({'issues': [issue]}, doc, [])['issues'][0]['status'] == 'open'
    issue['kind'] = 'conflict_candidate'
    with pytest.raises(ValueError, match='기준과 초안'):
        review({'issues': [issue]}, doc, [])


def test_graph_views_do_not_mix_draft_issue_into_revision(client):
    _, analysis = prepared(client)
    run = completed(client, analysis)
    result = client.get(f'/api/runs/{run["id"]}/graph?view=revision').json()
    assert all(not item['data']['issue_ids'] for item in result['nodes'] + result['edges'])
    assert all(item['data']['document_ref'] != ref(run['draft']) for item in result['nodes'])


class Transient(Exception):
    status_code = 429

class RetryProvider(MockProvider):
    def __init__(self, errors):
        self.errors = list(errors)
    async def call(self, role, stage, payload):
        if self.errors:
            raise self.errors.pop(0)
        return {'ok': True}, {'input_tokens': 7, 'output_tokens': 3}

@pytest.mark.asyncio
async def test_retry_budget_is_shared_and_only_transient():
    stats = usage()
    assert await call_provider(RetryProvider([Transient()]), 'writer', 'generate', {}, stats) == {'ok': True}
    assert stats['attempts'] == 2 and stats['retries'] == 1
    assert stats['input_tokens'] == 7
    with pytest.raises(ValueError, match='HTTP 429'):
        await call_provider(RetryProvider([Transient()]), 'reviewer', 'review', {}, stats)
    assert stats['attempts'] == 3 and stats['retries'] == 1

@pytest.mark.asyncio
async def test_timeout_is_not_retried():
    stats = usage()
    with pytest.raises(ValueError, match='자동 재시도하지'):
        await call_provider(RetryProvider([asyncio.TimeoutError()]), 'writer', 'generate', {}, stats)
    assert stats['attempts'] == 1 and stats['retries'] == 0

@pytest.mark.asyncio
async def test_six_attempt_hard_limit():
    stats = usage()
    stats['attempts'] = 6
    with pytest.raises(ValueError, match='6회'):
        await call_provider(RetryProvider([]), 'writer', 'generate', {}, stats)
    assert stats['attempts'] == 6


def test_restart_marks_running_as_interrupted(tmp_path):
    path = str(tmp_path/'restart.db')
    store = Store(path)
    store.put('run', 'r', {'id': 'r', 'status': 'running', 'stage': 'review'})
    store.put('analysis', 'a', {'id': 'a', 'status': 'running'})
    with TestClient(create_app(path, MockProvider())) as c:
        assert c.get('/api/runs/r').json()['status'] == 'interrupted'
    assert store.get('analysis', 'a')['status'] == 'interrupted'
    assert store.events('r')[-1]['status'] == 'interrupted'


def test_invalid_requests_use_string_detail(client):
    assert client.post('/api/sources', json={'text': ' '}).status_code == 422
    assert isinstance(client.post('/api/runs', json={}).json()['detail'], str)
    assert client.get('/api/runs/nonexistent').status_code == 404
    assert client.post('/api/sources', json={'text': 'a'*20001}).status_code == 422


def test_unusable_graph_container_does_not_destroy_valid_body():
    raw = body()
    raw.update(entities='not an array', relations=None)
    doc = document(raw, 'draft', 'ctx')
    assert doc['graph_status'] == 'unavailable'
    assert doc['passages'] == raw['passages']


class IntentProvider(MockProvider):
    async def call(self, role, stage, payload):
        result, tokens = await super().call(role, stage, payload)
        if role == 'reviewer':
            result['issues'][0]['kind'] = 'intent_question'
        return result, tokens


def test_intent_question_waits_for_author_without_automatic_rewrite(tmp_path):
    with TestClient(create_app(str(tmp_path/'intent.db'), IntentProvider())) as c:
        _, analysis = prepared(c)
        response = c.post('/api/runs', json={'request_id': 'intent', 'analysis_id': analysis['id']})
        for _ in range(150):
            run = c.get('/api/runs/'+response.json()['id']).json()
            if run['status'] != 'running':
                break
            time.sleep(.03)
        assert run['status'] == 'needs_attention'
        assert run['revision'] is None
        assert run['usage']['attempts'] == 3
        assert run['reviews'][-1]['issues'][0]['kind'] == 'intent_question'


def test_single_active_ai_operation_includes_analysis(tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    started, release = threading.Event(), threading.Event()
    class WaitingProvider(MockProvider):
        async def call(self, role, stage, payload):
            if role == 'analyst':
                started.set()
                while not release.is_set():
                    await asyncio.sleep(.01)
            return await super().call(role, stage, payload)
    with TestClient(create_app(str(tmp_path/'single.db'), WaitingProvider())) as c:
        source = c.post('/api/sources', json={'text': '별과 바다'}).json()
        url = '/api/sources/'+source['id']+'/analyze'
        with ThreadPoolExecutor() as executor:
            first = executor.submit(c.post, url, json={'request_id': 'first'})
            assert started.wait(2)
            try:
                second = c.post(url, json={'request_id': 'second'})
                assert second.status_code == 409
                assert '진행 중' in second.json()['detail']
            finally:
                release.set()
            assert first.result().status_code == 200


def test_mcp_rejects_unlisted_context_passages_and_is_readonly(tmp_path, monkeypatch):
    path = str(tmp_path/'mcp.db')
    store = Store(path)
    doc = document(body(), 'input', 'ctx')
    store.put('context', 'ctx', {'version': 'ctx', 'documents': [doc], 'constraints': []})
    monkeypatch.setenv('LORE_DB_PATH', path)
    assert get_passages('ctx', [ref(doc)+':p1'])[0]['text'] == '리나는 기다렸다.'
    with pytest.raises(ValueError, match='기준에 없는'):
        get_passages('ctx', ['invented:1:p1'])
    with pytest.raises(ValueError, match='스냅샷'):
        get_passages('missing', [])
    assert store.get('context', 'ctx')['documents'][0] == doc

@pytest.mark.asyncio
@pytest.mark.parametrize('status,text', [('incomplete', ''), ('completed', ''), ('completed', '{"invalid":true}')])
async def test_nonparseable_completed_response_preserves_observed_tokens(monkeypatch, status, text):
    from types import SimpleNamespace
    from backend.provider import OpenAIProvider
    monkeypatch.setenv('OPENAI_API_KEY', 'test-not-a-real-key')
    provider = OpenAIProvider()
    class Responses:
        async def create(self, **kwargs):
            assert kwargs['text']['format']['strict'] is True
            return SimpleNamespace(status=status, output_text=text, usage=SimpleNamespace(input_tokens=321, output_tokens=17))
    provider.client = SimpleNamespace(responses=Responses())
    stats = usage()
    with pytest.raises(ValueError):
        await call_provider(provider, 'writer', 'generate', {}, stats)
    assert stats['input_tokens'] == 321 and stats['output_tokens'] == 17
    assert stats['attempts'] == 1 and stats['calls'][0]['usage_known'] is True
    assert stats['calls'][0]['status'] == 'failed'

@pytest.mark.asyncio
async def test_cancellation_marks_interrupted_and_unknown_tokens():
    class WaitingProvider(MockProvider):
        async def call(self, *args):
            await asyncio.sleep(60)
    stats = usage()
    task = asyncio.create_task(call_provider(WaitingProvider(), 'writer', 'generate', {}, stats))
    await asyncio.sleep(.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stats['calls'][0]['status'] == 'interrupted'
    assert stats['calls'][0]['usage_known'] is False
    assert stats['calls'][0]['elapsed_seconds'] > 0
    assert stats['warnings']


def test_consumed_analysis_alias_binds_request_id_against_new_paid_run(client):
    _, first_analysis = prepared(client)
    first = completed(client, first_analysis)
    alias = client.post('/api/runs', json={'request_id': 'alias-key', 'analysis_id': first_analysis['id']})
    assert alias.json()['id'] == first['id']
    _, second_analysis = prepared(client, '다른 세계의 키워드')
    conflict = client.post('/api/runs', json={'request_id': 'alias-key', 'analysis_id': second_analysis['id']})
    assert conflict.status_code == 409
    assert len(client.get('/api/runs').json()['runs']) == 1


def test_trace_cleanup_cannot_overwrite_a_new_user_decision(tmp_path, monkeypatch):
    import threading
    import backend.main as main
    import backend.tracing as tracing
    closing, release = threading.Event(), threading.Event()
    async def start(*args, **kwargs):
        return 'metadata-only-test-root'
    async def span(*args, **kwargs):
        return None
    async def end(*args, **kwargs):
        closing.set()
        while not release.is_set():
            await asyncio.sleep(.01)
        return True
    monkeypatch.setattr(main, 'trace_start', start)
    monkeypatch.setattr(main, 'trace_end', end)
    monkeypatch.setattr(tracing, 'trace_span', span)
    with TestClient(create_app(str(tmp_path/'trace-race.db'), MockProvider())) as c:
        _, analysis = prepared(c)
        created = c.post('/api/runs', json={'request_id': 'trace-race', 'analysis_id': analysis['id'], 'trace': True}).json()
        assert closing.wait(3)
        try:
            assert c.get('/api/runs/'+created['id']).json()['status'] == 'running'
            assert c.post(f'/api/runs/{created["id"]}/decision', json={'status': 'accepted', 'reason': ''}).status_code == 409
        finally:
            release.set()
        for _ in range(100):
            if c.get('/api/runs/'+created['id']).json()['status'] != 'running':
                break
            time.sleep(.01)
        assert c.post(f'/api/runs/{created["id"]}/decision', json={'status': 'accepted', 'reason': ''}).status_code == 200
        assert c.get('/api/runs/'+created['id']).json()['decision']['status'] == 'accepted'

@pytest.mark.asyncio
async def test_openai_payload_contains_exact_reference_index_and_target(monkeypatch):
    import json
    from types import SimpleNamespace
    from backend.provider import OpenAIProvider
    monkeypatch.setenv('OPENAI_API_KEY', 'test-not-a-real-key')
    baseline = document(body(), 'input', 'ctx', document_id='real_baseline', version=3)
    target_body = body()
    target_body['entities'] = [{'id': 'e1', 'type': 'person', 'name': '리나', 'description': '', 'basis': 'explicit', 'evidence': [{'document_ref': 'self', 'passage_id': 'p1'}]}]
    target = document(target_body, 'draft', 'ctx', [baseline], document_id='real_target', version=7)
    provider = OpenAIProvider()
    captured = []
    class Responses:
        async def create(self, **kwargs):
            captured.append(kwargs)
            return SimpleNamespace(status='completed', output_text='{"candidates":[]}', model='actual-model-snapshot', usage=SimpleNamespace(input_tokens=1, output_tokens=1))
    provider.client = SimpleNamespace(responses=Responses())
    payload = {'context': {'version': 'ctx', 'documents': [baseline, target], 'constraints': []}, 'document': target}
    stats = usage()
    await call_provider(provider, 'reviewer', 'review', payload, stats)
    assert stats['calls'][0]['response_model'] == 'actual-model-snapshot'
    assert stats['calls'][0]['prompt_version'] == PROMPT_VERSION
    request = json.loads(captured[0]['input'][1]['content'])
    assert request['target_document_ref'] == 'real_target:7'
    assert request['reference_index'] == [
        {'document_ref': 'real_baseline:3', 'passage_ids': ['p1', 'p2'], 'qualified_entity_ids': []},
        {'document_ref': 'real_target:7', 'passage_ids': ['p1', 'p2'], 'qualified_entity_ids': ['real_target:7:e1']},
    ]
    assert 'reference_index' not in payload
    assert '그대로 복사' in captured[0]['input'][0]['content']
    assert captured[0]['reasoning'] == {'effort': 'low'}
    assert captured[0]['max_output_tokens'] == 8192


def test_generation_reference_index_has_self_target_and_only_existing_refs():
    from backend.provider import indexed_payload
    baseline = document(body(), 'input', 'ctx', document_id='source', version=2)
    payload = indexed_payload({'context': {'documents': [baseline]}})
    assert payload['target_document_ref'] == 'self'
    assert payload['reference_index'][0]['document_ref'] == 'source:2'
