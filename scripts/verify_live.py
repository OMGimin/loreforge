"""Explicit paid integration check using only public fictional inputs.

Run from repository root: .venv/bin/python scripts/verify_live.py --execute
Use --fixture-path with --review-cases or --cases-only for custom fixtures.
Use --keyword-continuation-only to skip the separate setting run. --trace opts in to
content-free LangSmith telemetry. Results are local, ignored runtime artifacts.
"""
import argparse
import asyncio
from collections import Counter
import hashlib
import json
import os
import sys
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURES = ROOT / "fixtures/review_cases.json"
KINDS = {"conflict_candidate", "internal_conflict", "missing_explanation", "intent_question"}


def load_cases(path):
    """Validate frozen expectations before any paid request is possible."""
    raw = Path(path).read_bytes()
    cases = json.loads(raw)
    if not isinstance(cases, list) or not cases:
        raise ValueError("Reviewer fixtures must be a nonempty list")
    ids = set()
    for case in cases:
        if not isinstance(case, dict) or not {"id", "title", "source", "passages", "expected_kind"} <= case.keys():
            raise ValueError("Reviewer fixture is missing required fields")
        if not isinstance(case["id"], str) or case["id"] in ids:
            raise ValueError("Reviewer fixture IDs must be unique strings")
        ids.add(case["id"])
        if not all(isinstance(case[k], str) and case[k].strip() for k in ("title", "source")):
            raise ValueError("Reviewer fixture title/source must be nonempty strings")
        if not isinstance(case["passages"], list) or not case["passages"] or not all(isinstance(p, str) and p.strip() for p in case["passages"]):
            raise ValueError("Reviewer fixture passages must be nonempty strings")
        kind = case["expected_kind"]
        if kind is not None and kind not in KINDS:
            raise ValueError("Unknown expected issue kind")
        count = case.get("expected_issue_count", int(kind is not None))
        if type(count) is not int or count < 0 or bool(count) != (kind is not None):
            raise ValueError("Expected issue count is inconsistent with expected kind")
        if "request" in case and not isinstance(case["request"], str):
            raise ValueError("Reviewer fixture request must be a string")
        for evidence in case.get("expected_evidence", []):
            if not isinstance(evidence, dict) or evidence.get("document") not in ("baseline", "draft"):
                raise ValueError("Invalid fixture evidence document")
            allowed = {"p1"} if evidence["document"] == "baseline" else {f"p{i+1}" for i in range(len(case["passages"]))}
            if evidence.get("passage_id") not in allowed:
                raise ValueError("Invalid fixture evidence passage")
    return cases, raw


def evaluate_case(case, result, baseline, draft):
    """Require valid references, exact issue counts, and expected passage coverage."""
    from backend.documents import review, ref
    entry = {**case, "raw_response": result, "baseline_ref": ref(baseline), "draft_ref": ref(draft)}
    try:
        validated = review(result, draft, [baseline])
    except ValueError as exc:
        return {**entry, "actual": result, "matches_fixture_expectation": False,
                "evidence_valid": False, "validation_error": str(exc)}
    actual = Counter(i["kind"] for i in validated["issues"])
    expected_count = case.get("expected_issue_count", int(case["expected_kind"] is not None))
    expected = Counter({case["expected_kind"]: expected_count}) if expected_count else Counter()
    missing = expected - actual
    extra = actual - expected
    locations = {(e["document_ref"], e["passage_id"]) for issue in validated["issues"] for e in issue["evidence"]}
    refs = {"baseline": ref(baseline), "draft": ref(draft)}
    required = {(refs[e["document"]], e["passage_id"]) for e in case.get("expected_evidence", [])}
    missing_evidence = sorted(required - locations)
    return {**entry, "actual": validated, "evidence_valid": True,
            "expected_kind_present_or_no_issue": case["expected_kind"] in actual if expected_count else not actual,
            "matches_fixture_expectation": not missing and not extra and not missing_evidence,
            "expected_issue_counts": dict(expected), "actual_issue_counts": dict(actual),
            "missing_issue_count": sum(missing.values()), "false_extra_issue_count": sum(extra.values()),
            "missing_expected_evidence": [{"document_ref": r, "passage_id": p} for r, p in missing_evidence]}


def git_state():
    def git(*args):
        result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, check=False)
        if result.returncode:
            return None
        return result.stdout
    head, branch, status = git("rev-parse", "HEAD"), git("branch", "--show-current"), git("status", "--porcelain=v1")
    return {"head": head.decode().strip() if head else None, "branch": branch.decode().strip() if branch else None,
            "status_porcelain": status.decode() if status is not None else None,
            "unstaged_diff_sha256": hashlib.sha256(git("diff", "--binary") or b"").hexdigest(),
            "staged_diff_sha256": hashlib.sha256(git("diff", "--cached", "--binary") or b"").hexdigest()}


def workflow_cases(args):
    cases = [("keywords", args.keyword_input)]
    if not (args.quick or args.keyword_continuation_only):
        cases.append(("setting", "미라는 밤에만 빛나는 편지를 배달한다. 편지는 해가 뜨면 글자가 지워진다. 미라는 기억을 다른 사람에게 복제할 수 없다."))
    return [] if args.cases_only else cases


def run_check(args):
    fixture_path = Path(args.fixture_path).resolve()
    fixture_cases, fixture_bytes = load_cases(fixture_path) if args.review_cases or args.cases_only else ([], b"")
    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
    if not os.getenv("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY is not configured. No request made.")
    # Test state must never share the everyday project DB.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    output = ROOT / "runtime" / f"verification-{stamp}"
    output.mkdir(parents=True, exist_ok=False)
    os.environ["LORE_DB_PATH"] = str(output / "live.db")
    os.environ["LORE_PROVIDER"] = "openai"
    from fastapi.testclient import TestClient
    from backend.main import create_app
    from backend.provider import OpenAIProvider, call_provider, usage, PROMPT_VERSION
    from backend.documents import document, ref
    report = {"created_at": stamp, "provider": "openai", "fixtures": "public fictional inputs", "runs": [], "review_cases": [], "trace_requested": args.trace,
              "prompt_version": PROMPT_VERSION, "git_state": git_state(), "command": sys.argv,
              "fixture_path": str(fixture_path) if fixture_cases else None,
              "fixture_sha256": hashlib.sha256(fixture_bytes).hexdigest() if fixture_cases else None,
              "workflow_inputs": [{"case": label, "text": text} for label, text in workflow_cases(args)]}
    if fixture_cases:
        (output / "reviewer-fixtures.json").write_bytes(fixture_bytes)

    def await_run(client, id):
        end = time.monotonic() + 620
        while time.monotonic() < end:
            value = client.get(f"/api/runs/{id}").json()
            if value["status"] != "running":
                return value
            time.sleep(.5)
        raise RuntimeError("verification deadline exceeded")

    def save():
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))

    save()

    with TestClient(create_app()) as client:
        report["models"] = client.get("/api/health").json()["models"]
        save()
        for label, text in workflow_cases(args):
            source = client.post("/api/sources", json={"text": text, "title": f"공개 검증: {label}"})
            source.raise_for_status()
            analysis = client.post(f"/api/sources/{source.json()['id']}/analyze", json={"request_id": uuid4().hex, "trace": args.trace})
            if analysis.status_code != 200:
                report["runs"].append({"case": label, "status": "analysis_failed", "detail": analysis.json().get("detail")})
                save()
                raise SystemExit(f"Analysis failed; see {output / 'report.json'}")
            result = client.post("/api/runs", json={"analysis_id": analysis.json()["id"], "request_id": uuid4().hex, "constraints": [], "request": "본문은 4~6문단, 700자 이내의 짧은 설정과 사건으로 구성해줘. 유지할 규칙과 주인공의 선택 이유를 구체적으로 적어줘.", "trace": args.trace})
            result.raise_for_status()
            run = await_run(client, result.json()["id"])
            run["verification_case"] = label
            report["runs"].append(run)
            save()
            print(json.dumps({"case": label, "status": run["status"], "attempts": run["usage"]["attempts"], "input_tokens": run["usage"]["input_tokens"], "output_tokens": run["usage"]["output_tokens"]}, ensure_ascii=False), flush=True)
            if run["status"] not in ("completed", "needs_attention"):
                raise SystemExit(f"Run failed; see {output / 'report.json'}")
            view = "revision" if run["revision"] else "draft"
            client.get(f"/api/runs/{run['id']}/graph?view={view}").raise_for_status()
            exported = client.get(f"/api/runs/{run['id']}/export")
            exported.raise_for_status()
            (output / f"{label}.md").write_text(exported.text)
            if label == "keywords" and not args.quick:
                client.post(f"/api/runs/{run['id']}/decision", json={"status": "accepted", "reason": "공개 입력의 이어쓰기 기능 검증을 위해 채택. 창작 품질의 객관적 보증은 아님."}).raise_for_status()
                continued = client.post("/api/runs", json={"accepted_run_id": run["id"], "request_id": uuid4().hex, "constraints": [], "request": "채택한 설정과 모순 없이 다음 사건을 4문단, 500자 이내로 작성해줘.", "trace": args.trace})
                continued.raise_for_status()
                next_run = await_run(client, continued.json()["id"])
                next_run["verification_case"] = "continuation"
                report["runs"].append(next_run)
                save()
                print(json.dumps({"case": "continuation", "status": next_run["status"], "attempts": next_run["usage"]["attempts"]}), flush=True)
                if next_run["status"] not in ("completed", "needs_attention"):
                    raise SystemExit(f"Continuation failed; see {output / 'report.json'}")

    if args.review_cases or args.cases_only:
        async def evaluate():
            provider = OpenAIProvider()
            report["reviewer_model"] = provider.models["reviewer"]
            save()
            for case in fixture_cases:
                baseline = document({"title": case["title"], "passages": [{"id": "p1", "text": case["source"]}], "entities": [], "relations": []}, "input", "evaluation")
                draft = document({"title": case["title"], "passages": [{"id": f"p{i+1}", "text": text} for i, text in enumerate(case["passages"])], "entities": [], "relations": []}, "draft", "evaluation")
                stats = usage()
                try:
                    result = await call_provider(provider, "reviewer", "review", {"context": {"version": "evaluation", "documents": [baseline], "constraints": []}, "document": draft, "request": case.get("request", "")}, stats, False)
                    # Persist the untouched structured response before evaluating it.
                    report["review_cases"].append({**case, "raw_response": result, "usage": stats, "evaluation_status": "pending"})
                    save()
                    entry = {**evaluate_case(case, result, baseline, draft), "usage": stats, "evaluation_status": "completed"}
                except ValueError as exc:
                    entry = {**case, "matches_fixture_expectation": False, "usage": stats, "evidence_valid": False, "call_error": str(exc), "baseline_ref": ref(baseline), "draft_ref": ref(draft), "evaluation_status": "call_failed"}
                if report["review_cases"] and report["review_cases"][-1]["id"] == case["id"]:
                    report["review_cases"][-1] = entry
                else:
                    report["review_cases"].append(entry)
                save()
                print(json.dumps({"case": case["id"], "counts": entry.get("actual_issue_counts"), "matches_fixture_expectation": entry["matches_fixture_expectation"], "false_extra_issue_count": entry.get("false_extra_issue_count")}), flush=True)
            await provider.client.close()
        asyncio.run(evaluate())
        report["review_evaluation_passed"] = all(entry["matches_fixture_expectation"] for entry in report["review_cases"])
        save()
    print(f"Local report: {output / 'report.json'}", flush=True)
    if report.get("review_evaluation_passed") is False:
        raise SystemExit(f"Reviewer fixture mismatch; see {output / 'report.json'}")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Make paid calls with the configured existing key")
    parser.add_argument("--review-cases", action="store_true")
    parser.add_argument("--cases-only", action="store_true", help="Only fixed reviewer fixtures; skip the three workflows")
    parser.add_argument("--quick", action="store_true", help="Only one keyword workflow; skip continuation and setting workflow")
    parser.add_argument("--keyword-continuation-only", action="store_true", help="Run keyword creation and its continuation; skip the separate setting workflow")
    parser.add_argument("--keyword-input", default="폭풍 도시, 기억을 보관하는 등대, 마지막 배달부", help="Override the public fictional keyword input")
    parser.add_argument("--fixture-path", default=str(DEFAULT_FIXTURES), help="Frozen reviewer fixture JSON; used with --review-cases or --cases-only")
    parser.add_argument("--trace", action="store_true", help="Opt in to content-free traces for the public input workflows")
    args = parser.parse_args(argv)
    if args.quick and args.keyword_continuation_only:
        parser.error("--quick and --keyword-continuation-only cannot be combined")
    if args.cases_only and (args.quick or args.keyword_continuation_only):
        parser.error("--cases-only cannot be combined with workflow selection flags")
    if not args.keyword_input.strip():
        parser.error("--keyword-input cannot be blank")
    if not args.execute:
        parser.error("--execute is required; this check makes paid API calls")
    return args


if __name__ == "__main__":
    run_check(parse_args())
