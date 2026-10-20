"""Offline checks for the paid verifier's selection, scoring and evidence report."""
import copy
import hashlib
import json
from types import SimpleNamespace

import pytest

from backend.documents import document, ref
from backend.provider import PROMPT_VERSION
from scripts import verify_live


def documents(case):
    baseline = document({"title": case["title"], "passages": [{"id": "p1", "text": case["source"]}], "entities": [], "relations": []}, "input", "evaluation")
    draft = document({"title": case["title"], "passages": [{"id": f"p{i+1}", "text": text} for i, text in enumerate(case["passages"])], "entities": [], "relations": []}, "draft", "evaluation")
    return baseline, draft


def issue(kind, baseline, draft, id="i1"):
    evidence = [{"document_ref": ref(draft), "passage_id": p["id"]} for p in draft["passages"]]
    if kind in ("conflict_candidate", "intent_question"):
        evidence.append({"document_ref": ref(baseline), "passage_id": "p1"})
    return {"id": id, "kind": kind, "reason": "검증용 판단", "suggestion": "검증용 제안", "evidence": evidence, "status": "open"}


def test_holdout_frozen_expectations_and_existing_fixtures():
    cases, raw = verify_live.load_cases(verify_live.ROOT / "fixtures/review_holdout.json")
    assert len(cases) == 6
    assert [c["expected_kind"] for c in cases] == ["conflict_candidate", "internal_conflict", "missing_explanation", "intent_question", None, None]
    assert all(c["rationale"] for c in cases)
    assert cases[3]["request"]
    assert json.loads(raw) == cases
    assert len(verify_live.load_cases(verify_live.DEFAULT_FIXTURES)[0]) == 8


@pytest.mark.parametrize("number", range(6))
def test_exact_expected_issues_pass(number):
    case = verify_live.load_cases(verify_live.ROOT / "fixtures/review_holdout.json")[0][number]
    baseline, draft = documents(case)
    raw = {"issues": [issue(case["expected_kind"], baseline, draft)] if case["expected_kind"] else []}
    saved = copy.deepcopy(raw)
    result = verify_live.evaluate_case(case, raw, baseline, draft)
    assert result["matches_fixture_expectation"]
    assert result["false_extra_issue_count"] == 0
    assert raw == saved == result["raw_response"]


def test_extra_same_kind_and_other_kind_do_not_pass_presence_check():
    case = verify_live.load_cases(verify_live.ROOT / "fixtures/review_holdout.json")[0][0]
    baseline, draft = documents(case)
    raw = {"issues": [issue("conflict_candidate", baseline, draft), issue("conflict_candidate", baseline, draft, "i2"), issue("intent_question", baseline, draft, "i3")]}
    result = verify_live.evaluate_case(case, raw, baseline, draft)
    assert result["expected_kind_present_or_no_issue"]
    assert not result["matches_fixture_expectation"]
    assert result["false_extra_issue_count"] == 2


def test_missing_expected_issue_and_false_positive_on_compatible_lore():
    cases = verify_live.load_cases(verify_live.ROOT / "fixtures/review_holdout.json")[0]
    baseline, draft = documents(cases[0])
    missing = verify_live.evaluate_case(cases[0], {"issues": []}, baseline, draft)
    assert not missing["matches_fixture_expectation"]
    assert missing["missing_issue_count"] == 1
    baseline, draft = documents(cases[5])
    extra = verify_live.evaluate_case(cases[5], {"issues": [issue("intent_question", baseline, draft)]}, baseline, draft)
    assert not extra["matches_fixture_expectation"]
    assert extra["false_extra_issue_count"] == 1


def test_correct_kind_with_fabricated_reference_fails_and_keeps_raw():
    case = verify_live.load_cases(verify_live.ROOT / "fixtures/review_holdout.json")[0][0]
    baseline, draft = documents(case)
    raw = {"issues": [issue("conflict_candidate", baseline, draft)]}
    raw["issues"][0]["evidence"][0]["document_ref"] = "invented:1"
    result = verify_live.evaluate_case(case, raw, baseline, draft)
    assert not result["matches_fixture_expectation"]
    assert not result["evidence_valid"]
    assert result["raw_response"] == raw


def test_existing_but_incomplete_expected_evidence_fails():
    case = verify_live.load_cases(verify_live.ROOT / "fixtures/review_holdout.json")[0][2]
    baseline, draft = documents(case)
    raw = {"issues": [issue("missing_explanation", baseline, draft)]}
    raw["issues"][0]["evidence"] = raw["issues"][0]["evidence"][:1]
    result = verify_live.evaluate_case(case, raw, baseline, draft)
    assert result["evidence_valid"]
    assert not result["matches_fixture_expectation"]
    assert result["missing_expected_evidence"] == [{"document_ref": ref(draft), "passage_id": "p2"}]


def test_keyword_continuation_selection_and_input_override():
    args = verify_live.parse_args(["--execute", "--keyword-continuation-only", "--keyword-input", "공개 가상 섬, 유리 시계"])
    assert verify_live.workflow_cases(args) == [("keywords", "공개 가상 섬, 유리 시계")]
    assert not args.quick  # The keyword run's continuation remains enabled.
    args = verify_live.parse_args(["--execute", "--cases-only"])
    assert verify_live.workflow_cases(args) == []
    args = verify_live.parse_args(["--execute"])
    assert [label for label, _ in verify_live.workflow_cases(args)] == ["keywords", "setting"]


@pytest.mark.parametrize("flags", [[], ["--execute", "--quick", "--keyword-continuation-only"], ["--execute", "--cases-only", "--quick"], ["--execute", "--keyword-input", " "]])
def test_cli_rejects_unpaid_or_ambiguous_modes(flags):
    with pytest.raises(SystemExit) as exc:
        verify_live.parse_args(flags)
    assert exc.value.code == 2


@pytest.mark.parametrize("failure", ["mismatch", "invalid_reference", "call_error", None])
def test_custom_fixture_report_and_exit_without_network(tmp_path, monkeypatch, failure):
    case = verify_live.load_cases(verify_live.ROOT / "fixtures/review_holdout.json")[0][3]
    fixture_path = tmp_path / "custom.json"
    fixture_bytes = json.dumps([case], ensure_ascii=False).encode()
    fixture_path.write_bytes(fixture_bytes)
    calls = []

    class FakeProvider:
        name = "openai"
        models = {role: "offline-fake" for role in ("analyst", "writer", "reviewer")}

        def __init__(self):
            self.client = SimpleNamespace(close=self.close)

        async def close(self):
            pass

        async def call(self, role, stage, payload):
            calls.append(payload)
            if failure == "call_error":
                raise ValueError("offline controlled failure")
            baseline, draft = payload["context"]["documents"][0], payload["document"]
            output = {"issues": [] if failure == "mismatch" else [issue("intent_question", baseline, draft)]}
            if failure == "invalid_reference":
                output["issues"][0]["evidence"][0]["passage_id"] = "missing"
            return output, {"input_tokens": 7, "output_tokens": 3, "response_model": "offline-fake-resolved"}

    monkeypatch.setattr("backend.provider.OpenAIProvider", FakeProvider)
    monkeypatch.setattr(verify_live, "ROOT", tmp_path)
    monkeypatch.setattr(verify_live, "git_state", lambda: {"head": "test-sha", "status_porcelain": " M scripts/verify_live.py\n"})
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test-placeholder")
    monkeypatch.setenv("LORE_DB_PATH", str(tmp_path / "unused.db"))
    monkeypatch.setenv("LORE_PROVIDER", "openai")
    args = verify_live.parse_args(["--execute", "--cases-only", "--fixture-path", str(fixture_path)])
    if failure:
        with pytest.raises(SystemExit, match="Reviewer fixture mismatch"):
            verify_live.run_check(args)
    else:
        verify_live.run_check(args)
    paths = list((tmp_path / "runtime").glob("verification-*/report.json"))
    assert len(paths) == 1
    report = json.loads(paths[0].read_text())
    assert len(calls) == 1
    assert calls[0]["request"] == case["request"]
    assert report["fixture_sha256"] == hashlib.sha256(fixture_bytes).hexdigest()
    assert (paths[0].parent / "reviewer-fixtures.json").read_bytes() == fixture_bytes
    assert report["reviewer_model"] == "offline-fake"
    assert report["prompt_version"] == PROMPT_VERSION
    assert report["git_state"]["head"] == "test-sha"
    entry = report["review_cases"][0]
    assert entry["id"] == case["id"]
    assert report["review_evaluation_passed"] is (failure is None)
    assert entry["usage"]["calls"][0]["prompt_version"] == PROMPT_VERSION
    if failure == "call_error":
        assert entry["call_error"] == "offline controlled failure"
    else:
        assert "raw_response" in entry
        assert entry["usage"]["calls"][0]["response_model"] == "offline-fake-resolved"


def test_bad_fixture_reference_rejected_before_paid_calls(tmp_path):
    case = verify_live.load_cases(verify_live.ROOT / "fixtures/review_holdout.json")[0][0]
    case["expected_evidence"][0]["passage_id"] = "invented"
    path = tmp_path / "bad.json"
    path.write_text(json.dumps([case]))
    with pytest.raises(ValueError, match="Invalid fixture evidence passage"):
        verify_live.load_cases(path)
