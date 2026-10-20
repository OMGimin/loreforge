"""End-to-end API contracts with explicit mocks; no paid provider calls."""
import copy
import hashlib
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from backend.main import create_app
from backend.provider import MockProvider
from backend.documents import document, ref, graph, export_markdown


def key():
    return uuid4().hex


def finished(client, id):
    for _ in range(150):
        run = client.get(f"/api/runs/{id}").json()
        if run["status"] != "running":
            return run
        time.sleep(.05)
    pytest.fail("run did not terminate")


def prepare(client, text="폭풍 도시, 기억의 등대, 마지막 배달부"):
    response = client.post("/api/sources", json={"text": text})
    assert response.status_code == 201, response.text
    source = response.json()
    params = {"request_id": key(), "trace": False}
    response = client.post(f"/api/sources/{source['id']}/analyze", json=params)
    assert response.status_code == 200, response.text
    return source, response.json(), params


def test_workflow_decision_continuation_restore_and_no_duplicate_calls(tmp_path):
    path = tmp_path / "acceptance.db"
    app = create_app(path, MockProvider())
    with TestClient(app) as client:
        source, analysis, analysis_params = prepare(client)
        repeated = client.post(f"/api/sources/{source['id']}/analyze", json=analysis_params).json()
        assert repeated["id"] == analysis["id"]
        assert repeated["usage"]["attempts"] == 1
        params = {"request_id": key(), "analysis_id": analysis["id"], "constraints": ["기억은 다른 사람에게 복제할 수 없다"], "request": "", "trace": False}
        initial = client.post("/api/runs", json=params)
        assert initial.status_code == 202, initial.text
        id = initial.json()["id"]
        run = finished(client, id)
        assert run["status"] == "completed", run["error"]
        assert run["usage"]["attempts"] == 5
        assert run["revision"]["version"] == 2
        assert len(run["reviews"]) == 2
        assert run["evidence_lookup"]["transport"] == "mcp-stdio"
        assert client.post("/api/runs", json=params).json()["id"] == id
        repeated_params = {**params, "request_id": key()}
        assert client.post("/api/runs", json=repeated_params).json()["id"] == id
        assert client.post("/api/runs", json={**params, "request": "다른 요청"}).status_code == 409
        assert client.get(f"/api/runs/{id}/graph?view=revision").status_code == 200
        assert client.get(f"/api/runs/{id}/graph?view=unknown").status_code == 422
        stream = client.get(f"/api/runs/{id}/events")
        assert "event: progress" in stream.text
        last_event = max(e["id"] for e in app.state.store.events(id))
        assert "event: progress" not in client.get(f"/api/runs/{id}/events", headers={"Last-Event-ID": str(last_event)}).text
        assert client.get(f"/api/runs/{id}").json()["usage"]["attempts"] == 5
        assert client.post("/api/runs", json={"request_id": key(), "accepted_run_id": id}).status_code == 409
        assert client.post(f"/api/runs/{id}/decision", json={"status": "deferred", "reason": "검토 중"}).status_code == 200
        assert client.post("/api/runs", json={"request_id": key(), "accepted_run_id": id}).status_code == 409
        adopted = client.post(f"/api/runs/{id}/decision", json={"status": "accepted", "reason": "흐름 검증"}).json()
        assert adopted["draft"]["decision"] == "pending"
        assert adopted["revision"]["decision"] == "accepted"
        markdown = client.get(f"/api/runs/{id}/export")
        assert markdown.status_code == 200
        assert "## passage-" in markdown.text and "## entity-" in markdown.text
        assert "text/markdown" in markdown.headers["content-type"]
        next_run = client.post("/api/runs", json={"request_id": key(), "accepted_run_id": id}).json()
        next_run = finished(client, next_run["id"])
        assert next_run["status"] == "completed", next_run["error"]
        refs = [ref(d) for d in next_run["context"]["documents"]]
        assert ref(adopted["revision"]) in refs
        assert ref(adopted["draft"]) not in refs
        restored_source = client.get(f"/api/sources/{source['id']}").json()
        assert restored_source["text"] == source["text"]
        assert restored_source["sha256"] == hashlib.sha256(source["text"].encode()).hexdigest()
        before = copy.deepcopy(client.get(f"/api/runs/{id}").json())
    with TestClient(create_app(path, MockProvider())) as client:
        assert client.get(f"/api/runs/{id}").json() == before
        assert len(client.get("/api/runs").json()["runs"]) == 2


def test_input_errors_are_actionable_strings(tmp_path):
    with TestClient(create_app(tmp_path / "validation.db", MockProvider())) as client:
        for data in ({"text": " "}, {"text": "x" * 20001}, {"text": "x", "extra": True}):
            response = client.post("/api/sources", json=data)
            assert response.status_code == 422
            assert isinstance(response.json()["detail"], str)
        assert client.post("/api/runs", json={"request_id": key()}).status_code == 422
        assert client.get("/api/runs/missing").status_code == 404


def test_restarted_inflight_is_interrupted_without_execution(tmp_path):
    path = tmp_path / "restart.db"
    app = create_app(path, MockProvider())
    app.state.store.put("run", "unfinished", {"id": "unfinished", "status": "running", "stage": "review", "usage": {"attempts": 2}})
    with TestClient(create_app(path, MockProvider())) as client:
        run = client.get("/api/runs/unfinished").json()
        assert run["status"] == "interrupted"
        assert run["usage"]["attempts"] == 2


def test_graph_limits_do_not_truncate_storage_or_combine_versions():
    ev = [{"document_ref": "self", "passage_id": "p1"}]
    raw = {"title": "큰 그래프", "passages": [{"id": "p1", "text": "공개 가상 그래프 검증 자료"}],
           "entities": [{"id": f"e{i}", "type": "person", "name": "동명이인", "description": str(i), "basis": "explicit", "evidence": ev} for i in range(35)],
           "relations": [{"id": f"r{i}", "source": f"e{i%35}", "target": f"e{(i+1)%35}", "type": "related_to", "label": "연결", "condition": "", "basis": "explicit", "evidence": ev} for i in range(65)]}
    draft = document(raw, "draft", "ctx")
    revision = document(raw, "revision", "ctx", document_id=draft["document_id"], version=2)
    run = {"draft": draft, "revision": revision, "context": {"documents": []}, "reviews": []}
    result = graph(run, "revision")
    assert len(result["nodes"]) == 35 and len(result["edges"]) == 65
    assert all(node["data"]["document_ref"] == ref(revision) for node in result["nodes"])
    assert len({node["data"]["id"] for node in result["nodes"]}) == 35


def test_graph_malformed_ids_are_partial_and_body_remains():
    raw = {"title": "본문 보존", "passages": [{"id": "p1", "text": "본문은 유효하다"}], "entities": [{"id": []}], "relations": [{"id": {}}]}
    result = document(raw, "draft", "ctx")
    assert result["passages"] == raw["passages"]
    assert result["graph_status"] == "unavailable"
    assert result["warnings"]


def test_body_authored_markdown_anchor_is_not_an_export_validation_error():
    doc = document({"title": "링크 원문", "passages": [{"id": "p1", "text": "원문에 [목차](#not-generated)와 <script>가 있다."}], "entities": [], "relations": []}, "draft", "ctx")
    run = {"draft": doc, "revision": None, "context": {"documents": []}, "reviews": [], "provider": "mock", "decision": {"status": "pending", "reason": ""}}
    output = export_markdown(run)
    assert "&lt;script&gt;" in output
