"""No-network regression coverage for unresolved author choices and provenance."""
import copy
import time

from fastapi.testclient import TestClient

from backend.documents import ref, merge_pending_issues, pending_issues_length
from backend.main import create_app
from backend.provider import MockProvider


class MixedProvider(MockProvider):
    def __init__(self):
        self.payloads = []

    async def call(self, role, stage, payload):
        self.payloads.append((role, stage, copy.deepcopy(payload)))
        result, tokens = await super().call(role, stage, payload)
        if stage == 'review':
            result['issues'].append({
                'id': 'author-choice', 'kind': 'intent_question',
                'reason': '마지막 송신을 성공으로 남길지 실패로 남길지 선택이 필요합니다.',
                'suggestion': '작가가 결말의 방향을 선택해주세요.', 'status': 'open',
                'evidence': [{'document_ref': ref(payload['document']), 'passage_id': 'p03'}],
            })
        return result, tokens


def start(client, request='송신의 결말은 아직 결정하지 말아줘'):
    source = client.post('/api/sources', json={'text': '공개 가상 설정: 폭풍 중 송신할 수 없다.'}).json()
    analysis = client.post(f'/api/sources/{source["id"]}/analyze', json={'request_id': 'a-' + source['id']}).json()
    response = client.post('/api/runs', json={'request_id': 'r-' + source['id'], 'analysis_id': analysis['id'], 'request': request})
    assert response.status_code == 202, response.text
    return wait_run(client, response.json()['id'])


def wait_run(client, run_id):
    for _ in range(200):
        run = client.get('/api/runs/' + run_id).json()
        if run['status'] != 'running':
            assert run['status'] in ('completed', 'needs_attention'), run
            return run
        time.sleep(.02)
    raise AssertionError('Mock run did not finish')


def test_mixed_questions_survive_actionable_revision_and_empty_rereview(tmp_path):
    provider = MixedProvider()
    with TestClient(create_app(str(tmp_path / 'mixed.db'), provider)) as client:
        run = start(client)
        assert run['revision'] is not None
        assert run['usage']['attempts'] == 5
        assert run['reviews'][-1]['issues'] == []
        assert run['status'] == 'needs_attention'
        record = run['pending_issues'][0]
        assert record['origin_run_id'] == run['id']
        assert record['document_ref'] == ref(run['draft'])
        assert record['issue']['id'] == 'author-choice'
        assert record['issue']['status'] == 'open'
        assert record['evidence_snapshots'] == [{
            'document_ref': ref(run['draft']), 'passage_id': 'p03',
            'text': run['draft']['passages'][2]['text'],
        }]
        writer = next(payload for role, stage, payload in provider.payloads if stage == 'revise')
        assert [i['kind'] for i in writer['review']['issues']] == ['missing_explanation']
        assert writer['protected_unresolved_issues'] == run['pending_issues']
        rereview = next(payload for role, stage, payload in provider.payloads if stage == 'review_revision')
        assert rereview['context']['unresolved_issues'] == run['pending_issues']


def test_reviewer_receives_same_request_for_draft_and_revision(tmp_path):
    provider = MixedProvider()
    with TestClient(create_app(str(tmp_path / 'request.db'), provider)) as client:
        run = start(client)
        reviews = [payload for role, stage, payload in provider.payloads if role == 'reviewer']
        assert len(reviews) == 2
        assert all(payload.get('request') == run['request'] for payload in reviews)


def test_export_does_not_duplicate_current_review_as_historical_pending(tmp_path):
    class OnlyQuestion(MixedProvider):
        async def call(self, role, stage, payload):
            result, tokens = await super().call(role, stage, payload)
            if role == 'reviewer':
                result['issues'] = [i for i in result['issues'] if i['kind'] == 'intent_question']
            return result, tokens
    with TestClient(create_app(str(tmp_path / 'only-question.db'), OnlyQuestion())) as client:
        run = start(client)
        assert run['revision'] is None and run['pending_issues']
        exported = client.get(f'/api/runs/{run["id"]}/export').text
        assert exported.count(run['pending_issues'][0]['issue']['reason']) == 1
        assert 'needs_attention' in exported


def test_pending_snapshot_survives_accept_continuation_restart_and_export(tmp_path):
    db = str(tmp_path / 'persist.db')
    provider = MixedProvider()
    with TestClient(create_app(db, provider)) as client:
        run = start(client)
        accepted = client.post(f'/api/runs/{run["id"]}/decision', json={'status': 'accepted', 'reason': '질문은 보류한 채 다음 사건을 살펴본다'}).json()
        assert accepted['pending_issues'] == run['pending_issues']
        response = client.post('/api/runs', json={'request_id': 'next', 'accepted_run_id': run['id']})
        assert response.status_code == 202, response.text
        continued = wait_run(client, response.json()['id'])
        inherited = continued['context']['unresolved_issues']
        assert inherited == run['pending_issues']
        assert inherited[0] in continued['pending_issues']
        assert ref(run['draft']) not in [ref(doc) for doc in continued['context']['documents']]
        assert ref(run['revision']) in [ref(doc) for doc in continued['context']['documents']]
        continuation_writer = [payload for role, stage, payload in provider.payloads if stage == 'generate'][-1]
        assert continuation_writer['context']['unresolved_issues'] == inherited
        client.post(f'/api/runs/{run["id"]}/decision', json={'status': 'deferred', 'reason': '이전 결정 변경'})
        assert client.get('/api/runs/' + continued['id']).json()['context']['unresolved_issues'] == inherited
        exported = client.get(f'/api/runs/{continued["id"]}/export')
        assert exported.status_code == 200
        assert '## 보존한 미해결 쟁점' in exported.text
        assert ref(run['draft']) in exported.text
        assert inherited[0]['evidence_snapshots'][0]['text'] in exported.text
    with TestClient(create_app(db, MixedProvider())) as client:
        restored = client.get('/api/runs/' + continued['id']).json()
        assert restored['context']['unresolved_issues'] == inherited
        assert restored['pending_issues'] == continued['pending_issues']


def test_legacy_final_open_issues_are_carried_and_budgeted(tmp_path):
    app = create_app(str(tmp_path / 'legacy.db'), MixedProvider())
    with TestClient(app) as client:
        run = start(client)
        # Simulate an older stored run, whose final review was still open.
        final_review = copy.deepcopy(run['reviews'][0])
        final_review['document_ref'] = ref(run['revision'])
        final_review['issues'] = [final_review['issues'][0]]
        final_review['issues'][0]['evidence'][0]['document_ref'] = ref(run['revision'])
        run['reviews'].append(final_review)
        run.pop('pending_issues', None)
        run['context'].pop('unresolved_issues', None)
        run['decision']['status'] = 'accepted'
        run['revision']['decision'] = 'accepted'
        app.state.store.put('run', run['id'], run)
        response = client.post('/api/runs', json={'request_id': 'legacy-next', 'accepted_run_id': run['id']})
        assert response.status_code == 202, response.text
        continued = wait_run(client, response.json()['id'])
        inherited = continued['context']['unresolved_issues']
        assert len(inherited) == 1
        assert inherited[0]['document_ref'] == ref(run['revision'])
        assert inherited[0]['issue']['id'] == 'i1'
        assert inherited[0]['issue']['kind'] == 'missing_explanation'
        assert inherited[0] in continued['pending_issues']
        # Retained question text must not bypass the inherited context limit.
        run['reviews'][-1]['issues'][0]['reason'] = '질문' * 10001
        app.state.store.put('run', run['id'], run)
        rejected = client.post('/api/runs', json={'request_id': 'too-large', 'accepted_run_id': run['id']})
        assert rejected.status_code == 422


def test_pending_records_dedupe_keep_snapshot_independent_and_count_unique_text():
    record = {'origin_run_id': 'original-run', 'document_ref': 'draft:1',
              'issue': {'id': 'q1', 'reason': '동일한 질문', 'suggestion': '동일한 질문'},
              'evidence_snapshots': [{'document_ref': 'draft:1', 'passage_id': 'p1', 'text': '동일한 질문'}]}
    merged = merge_pending_issues([record], [copy.deepcopy(record)])
    assert len(merged) == 1
    assert pending_issues_length(merged) == len('동일한 질문')
    record['evidence_snapshots'][0]['text'] = '변경된 원본'
    assert merged[0]['evidence_snapshots'][0]['text'] == '동일한 질문'
