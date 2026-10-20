"""Read back only the LangSmith traces referenced by a local public-input report.

No model requests. Requires a PAT/service key that can read the recorded runs.
Run: .venv/bin/python scripts/verify_trace.py runtime/verification-.../report.json
"""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import sqlite3
import time

from dotenv import load_dotenv

FIELDS = {"provider", "operation", "analysis_reused", "role", "model", "response_model",
          "prompt_version", "status", "attempt", "duration_seconds", "input_tokens",
          "output_tokens", "transport", "passage_count", "next_stage", "revision_used",
          # SDK/server additions observed in the pinned client and remote records.
          "LANGSMITH_PROJECT", "revision_id", "ls_run_depth"}


def inspect_span(span):
    metadata = (span.extra or {}).get("metadata", {})
    outputs = span.outputs or {}
    errors = []
    if span.inputs:
        errors.append("Trace inputs must be empty")
    if set(metadata) - FIELDS or set(outputs) - FIELDS:
        errors.append("Unexpected content fields in metadata/outputs")
    if span.end_time is None:
        errors.append("Trace is not closed")
    return {"id": str(span.id), "name": span.name, "parent_run_id": str(span.parent_run_id) if span.parent_run_id else None,
            "metadata": {k: v for k, v in metadata.items() if k in FIELDS},
            "outputs": {k: v for k, v in outputs.items() if k in FIELDS}, "errors": errors}


def collect_roots(report, objects):
    """Require complete local coverage before checking any remote trace."""
    errors, roots = [], {}
    stored = {kind: {} for kind in ("analysis", "run")}
    for kind, item in objects:
        stored[kind][item["id"]] = item
    reported = {}
    source_ids, analysis_ids = set(), set()
    if report.get("trace_requested") is not True:
        errors.append("Report did not explicitly request tracing")
    if not report.get("runs"):
        errors.append("Report has no traced workflow runs")
    for item in report.get("runs", []):
        id, source_id = item.get("id"), item.get("source_id")
        if not id or not source_id:
            errors.append("Report run is missing its stored identity/source (possibly analysis_failed)")
            continue
        if id in reported:
            errors.append(f"Report run is duplicated: {id}")
        reported[id] = item
        source_ids.add(source_id)
        if item.get("analysis_id"):
            analysis_ids.add(item["analysis_id"])
        if item.get("trace") is not True:
            errors.append(f"Report run did not request tracing: {id}")
    if set(reported) != set(stored["run"]):
        errors.append("Report run IDs differ from stored run IDs")
    # A continuation still requires the original source analysis root once.
    source_analyses = {id for id, item in stored["analysis"].items() if item.get("source_id") in source_ids}
    expected_analyses = source_analyses | analysis_ids
    if expected_analyses != set(stored["analysis"]):
        errors.append("Report source analysis IDs differ from stored analysis IDs")
    if any(not any(item.get("source_id") == source_id for item in stored["analysis"].values()) for source_id in source_ids):
        errors.append("A reported source has no stored analysis")
    for id, item in reported.items():
        local = stored["run"].get(id)
        if local is None:
            continue
        for field in ("source_id", "analysis_id", "accepted_run_id", "status"):
            if item.get(field) != local.get(field):
                errors.append(f"Report/stored run {field} differ: {id}")
        for field in ("trace_id", "calls"):
            if item.get("usage", {}).get(field) != local.get("usage", {}).get(field):
                errors.append(f"Report/stored run usage {field} differ: {id}")
        analysis_id = local.get("analysis_id")
        if analysis_id and stored["analysis"].get(analysis_id, {}).get("source_id") != local.get("source_id"):
            errors.append(f"Run references a missing or different-source analysis: {id}")
    for kind, items in stored.items():
        for id, item in items.items():
            stats = item.get("usage", {})
            trace_id = stats.get("trace_id")
            if not trace_id:
                errors.append(f"Stored {kind} is missing its trace ID: {id}")
            if kind == "run" and item.get("trace") is not True:
                errors.append(f"Stored run did not request tracing: {id}")
            if item.get("status") not in ("completed", "needs_attention"):
                errors.append(f"Stored {kind} did not complete successfully: {id}")
            variants = [item, *([reported[id]] if id in reported else [])]
            if any(value.get("usage", {}).get("trace_warnings") or any(
                    term in warning.lower() for warning in value.get("warnings", [])
                    for term in ("langsmith", "trace", "추적")) for value in variants):
                errors.append(f"Local trace delivery warning: {id}")
            calls = stats.get("calls")
            if not isinstance(calls, list):
                errors.append(f"Stored {kind} has no call records: {id}")
                calls = []
            # Initial analysis is copied into job usage but has its own root.
            if kind == "run" and item.get("analysis_id"):
                calls = [c for c in calls if c["stage"] != "analyze"]
            if trace_id:
                if trace_id in roots:
                    errors.append(f"Multiple local records share one trace ID: {id}")
                roots[trace_id] = {"local_id": id, "calls": calls, "status": item["status"], "is_job": kind == "run"}
    return roots, errors


def main(path):
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    report = json.loads(path.read_text())
    with sqlite3.connect((path.parent / "live.db").as_uri() + "?mode=ro", uri=True) as db:
        objects = [(kind, json.loads(raw)) for kind, raw in db.execute("SELECT kind, value FROM objects WHERE kind IN ('analysis','run')")]
    roots, coverage_errors = collect_roots(report, objects)
    result = {"report": str(path), "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "traces": [], "coverage_errors": coverage_errors, "passed": not coverage_errors}
    destination = path.parent / "trace-verification.json"
    if coverage_errors:
        destination.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(f"Remote trace verification: FAIL; local coverage errors={len(coverage_errors)}; {destination}")
        raise SystemExit(1)
    if not os.getenv("LANGSMITH_API_KEY"):
        raise SystemExit("LANGSMITH_API_KEY is missing; no request made")
    from langsmith import Client
    client = Client(timeout_ms=10000)
    for trace_id, local in roots.items():
        try:
            remote = None
            for attempt in range(3):
                remote = client.read_run(trace_id, load_child_runs=True)
                model_children = [c for c in (remote.child_runs or []) if (c.extra or {}).get("metadata", {}).get("role")]
                if remote.end_time and len(model_children) >= len(local["calls"]):
                    break
                if attempt < 2:
                    time.sleep(2)
            spans = [inspect_span(s) for s in [remote, *(remote.child_runs or [])]]
            errors = [error for s in spans for error in s["errors"]]
            expected = Counter((c["stage"], c["role"], c["model"], c["input_tokens"], c["output_tokens"]) for c in local["calls"])
            actual = Counter((s["name"].removeprefix("LoreForge/"), s["metadata"].get("role"), s["metadata"].get("model"), s["metadata"].get("input_tokens"), s["metadata"].get("output_tokens")) for s in spans if s["metadata"].get("role"))
            if actual != expected:
                errors.append("Remote model calls differ from local stage/model/token records")
            if spans[0]["outputs"].get("status") != local["status"]:
                errors.append("Remote terminal status differs from local record")
            if local["is_job"] and not any(s["name"] == "LoreForge/mcp/get_passages" for s in spans):
                errors.append("MCP span missing")
            if local["is_job"] and not any(s["name"] == "LoreForge/branch" for s in spans):
                errors.append("Branch span missing")
            url = client.get_run_url(run=remote)
            result["traces"].append({"local_id": local["local_id"], "trace_id": trace_id, "url": url, "spans": spans, "errors": errors})
            result["passed"] &= not errors
        except Exception as exc:
            result["passed"] = False
            result["traces"].append({"local_id": local["local_id"], "trace_id": trace_id, "error_type": type(exc).__name__})
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"Remote trace verification: {'PASS' if result['passed'] else 'FAIL'}; roots={len(roots)}; {destination}")
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    main(parser.parse_args().report.resolve())
