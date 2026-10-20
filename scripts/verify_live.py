"""Explicit paid integration check using only public fictional inputs.

Run from repository root: .venv/bin/python scripts/verify_live.py --execute
Use --review-cases for the eight fixed reviewer fixtures; --trace opts in to
content-free LangSmith telemetry. Results are local, ignored runtime artifacts.
"""
import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv


def run_check(args):
    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
    if not os.getenv("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY is not configured. No request made.")
    # Test state must never share the everyday project DB.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = Path("runtime") / f"verification-{stamp}"
    output.mkdir(parents=True, exist_ok=False)
    os.environ["LORE_DB_PATH"] = str(output / "live.db")
    os.environ["LORE_PROVIDER"] = "openai"
    from fastapi.testclient import TestClient
    from backend.main import create_app
    from backend.provider import OpenAIProvider, call_provider, usage
    from backend.documents import document, review, ref
    report = {"created_at": stamp, "provider": "openai", "fixtures": "public fictional inputs", "runs": [], "review_cases": [], "trace_requested": args.trace}

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

    with TestClient(create_app()) as client:
        report["models"] = client.get("/api/health").json()["models"]
        cases = (
            ("keywords", "폭풍 도시, 기억을 보관하는 등대, 마지막 배달부"),
            ("setting", "미라는 밤에만 빛나는 편지를 배달한다. 편지는 해가 뜨면 글자가 지워진다. 미라는 기억을 다른 사람에게 복제할 수 없다."),
        )
        for label, text in ([] if args.cases_only else cases[:1] if args.quick else cases):
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
            for case in json.loads(Path("fixtures/review_cases.json").read_text()):
                baseline = document({"title": case["title"], "passages": [{"id": "p1", "text": case["source"]}], "entities": [], "relations": []}, "input", "evaluation")
                draft = document({"title": case["title"], "passages": [{"id": f"p{i+1}", "text": text} for i, text in enumerate(case["passages"])], "entities": [], "relations": []}, "draft", "evaluation")
                stats = usage()
                result = await call_provider(provider, "reviewer", "review", {"context": {"version": "evaluation", "documents": [baseline], "constraints": []}, "document": draft}, stats, False)
                try:
                    validated = review(result, draft, [baseline])
                    kinds = [i["kind"] for i in validated["issues"]]
                    expected = case["expected_kind"]
                    match = expected in kinds if expected else not kinds
                    entry = {**case, "actual": validated, "expected_kind_present_or_no_issue": match, "usage": stats, "evidence_valid": True}
                except ValueError as exc:
                    kinds, match = [], False
                    entry = {**case, "actual": result, "expected_kind_present_or_no_issue": False, "usage": stats, "evidence_valid": False, "validation_error": str(exc), "baseline_ref": ref(baseline), "draft_ref": ref(draft)}
                report["review_cases"].append(entry)
                save()
                print(json.dumps({"case": case["id"], "kinds": kinds, "matches_fixture_expectation": match}), flush=True)
            await provider.client.close()
        asyncio.run(evaluate())
    print(f"Local report: {output / 'report.json'}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Make paid calls with the configured existing key")
    parser.add_argument("--review-cases", action="store_true")
    parser.add_argument("--cases-only", action="store_true", help="Only fixed reviewer fixtures; skip the three workflows")
    parser.add_argument("--quick", action="store_true", help="Only one keyword workflow; skip continuation and setting workflow")
    parser.add_argument("--trace", action="store_true", help="Opt in to content-free traces for the public input workflows")
    args = parser.parse_args()
    if not args.execute:
        parser.error("--execute is required; this check makes paid API calls")
    run_check(args)
