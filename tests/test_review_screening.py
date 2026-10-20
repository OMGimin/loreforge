"""Review candidates must survive a source-backed counterevidence check."""
import copy
import pytest
from backend.documents import document, ref, review


def setup():
    doc = document({'title':'공개 검토 사례','passages':[
        {'id':'p1','text':'첫 결투는 개인의 무력을 겨룬다.'},
        {'id':'p2','text':'다음 결전은 진영과 강호관의 대립이다.'},
        {'id':'p3','text':'같은 인물과 다시 싸울 때에는 패배한 진영의 반격이라는 별도 사건이 발생한다.'}],
        'entities':[], 'relations':[]}, 'input', None)
    issue = dict(id='i1',kind='missing_explanation',reason='두 결전의 차이를 설명하지 않았다.',suggestion='차이를 설명한다.',evidence=[dict(document_ref=ref(doc),passage_id='p1'),dict(document_ref=ref(doc),passage_id='p2')],status='open')
    return doc,issue


def candidate(issue, verdict='explained', counter='p2'):
    return dict(issue=issue,verdict=verdict,counter_evidence=[] if counter is None else [dict(document_ref=issue['evidence'][0]['document_ref'],passage_id=counter)],rationale='원문은 개인 무력과 진영 대립을 구분한다.')


def test_existing_explanation_is_withheld_and_audit_preserves_exact_evidence():
    doc, issue=setup()
    raw={'candidates':[candidate(issue)]}
    before=copy.deepcopy(raw)
    result=review(raw,doc,[doc])
    assert result['issues']==[]
    assert result['screening'][0]['verdict']=='explained'
    assert result['screening'][0]['counter_evidence'][0]['passage_id']=='p2'
    assert result['screening'][0]['issue']==issue
    assert raw==before


def test_unresolved_claim_survives_alongside_dismissed_optional_detail():
    doc,issue=setup()
    unresolved=copy.deepcopy(issue); unresolved.update(id='i2',reason='시간 순서가 여전히 충돌한다.',kind='internal_conflict')
    result=review({'candidates':[candidate(issue,'optional',None),candidate(unresolved,'unresolved','p3')]},doc,[doc])
    assert [i['id'] for i in result['issues']]==['i2']
    assert len(result['screening'])==2


@pytest.mark.parametrize('counter',[None,'invented'])
def test_explained_verdict_requires_existing_counterevidence(counter):
    doc,issue=setup()
    with pytest.raises(ValueError): review({'candidates':[candidate(issue,counter=counter)]},doc,[doc])


def test_duplicate_candidate_ids_and_empty_rationale_cannot_hide_findings():
    doc,issue=setup()
    with pytest.raises(ValueError): review({'candidates':[candidate(issue),candidate(issue)]},doc,[doc])
    item=candidate(issue);item['rationale']=''
    with pytest.raises(ValueError): review({'candidates':[item]},doc,[doc])


def test_legacy_saved_review_still_reads_without_fabricated_screening():
    doc,issue=setup()
    result=review({'issues':[issue]},doc,[doc])
    assert result['issues']==[issue]
    assert result.get('screening',[])==[]


def test_screened_review_finishes_without_writer_and_exports_resolution(tmp_path):
    import time
    from fastapi.testclient import TestClient
    from backend.main import create_app
    from backend.provider import MockProvider
    class ScreenedProvider(MockProvider):
        async def call(self, role, stage, payload):
            if role == 'analyst': return await super().call(role, stage, payload)
            assert role == 'reviewer'
            doc=payload['document']
            issue=dict(id='i1',kind='missing_explanation',reason='이동 설명 없음',suggestion='이동 설명 추가',evidence=[dict(document_ref=ref(doc),passage_id='p001')],status='open')
            return {'candidates':[candidate(issue,counter='p002')]},{'input_tokens':1,'output_tokens':1}
    with TestClient(create_app(str(tmp_path/'screen.db'),ScreenedProvider())) as client:
        source=client.post('/api/sources',json={'title':'검토 필터','text':'리나는 방 안에 있었다.\n\n문이 열려 리나는 걸어 나왔다.'}).json()
        analysis=client.post(f'/api/sources/{source["id"]}/analyze',json={'request_id':'analysis'}).json()
        result=client.post('/api/runs',json={'request_id':'review','mode':'review','analysis_id':analysis['id']}).json()
        for _ in range(200):
            result=client.get('/api/runs/'+result['id']).json()
            if result['status']!='running':break
            time.sleep(.02)
        assert result['status']=='completed'
        assert result['reviews'][0]['issues']==[]
        assert len(result['reviews'][0]['screening'])==1
        assert result['usage']['attempts']==2
        report=client.get('/api/runs/'+result['id']+'/export')
        assert report.status_code==200
        assert '문제 목록에서 제외한 후보' in report.text and '#passage-p002' in report.text
