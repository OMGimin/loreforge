"""Document validation, evidence resolution, graph and safe Markdown export."""
import copy
import re
from collections import Counter
from pydantic import ValidationError
from .models import Entity, Relation, StoryOutput, AnalysisOutput, ReviewOutput
from .store import new_id


def ref(document):
    return f'{document["document_id"]}:{document["version"]}'


def passage_map(documents):
    return {(ref(d), p["id"]): p["text"] for d in documents for p in d["passages"]}


def issue_records(run, reviewed, kinds=None):
    """Keep original review provenance without making rejected text canonical."""
    documents = [*run["context"]["documents"], *[run[key] for key in ("draft", "revision") if run.get(key)]]
    mapping = passage_map(documents)
    records = []
    for issue in reviewed["issues"]:
        if issue["status"] != "open" or (kinds is not None and issue["kind"] not in kinds):
            continue
        snapshots = []
        for evidence in issue["evidence"]:
            snapshot = {**evidence, "text": mapping[(evidence["document_ref"], evidence["passage_id"])]}
            if snapshot not in snapshots:
                snapshots.append(snapshot)
        records.append({"origin_run_id": run["id"], "document_ref": reviewed["document_ref"],
                        "issue": copy.deepcopy(issue), "evidence_snapshots": snapshots})
    return records


def merge_pending_issues(*groups):
    records, seen = [], set()
    for group in groups:
        for record in group:
            key = (record["origin_run_id"], record["document_ref"], record["issue"]["id"])
            if key not in seen:
                seen.add(key)
                records.append(copy.deepcopy(record))
    return records


def pending_issues_length(records):
    # Repeated reason/suggestion/evidence text within a record counts only once.
    return sum(sum(len(text) for text in {record["issue"]["reason"], record["issue"]["suggestion"],
                                        *[e["text"] for e in record["evidence_snapshots"]]}) for record in records)


def valid_evidence(evidence, document, context, allow_empty=False):
    mapping = passage_map([*context, document])
    normalized = [{**e, "document_ref": ref(document) if e["document_ref"] == "self" else e["document_ref"]} for e in evidence]
    if not normalized and not allow_empty:
        raise ValueError("근거가 없습니다")
    if any((e["document_ref"], e["passage_id"]) not in mapping for e in normalized):
        raise ValueError("존재하지 않거나 허용되지 않은 문단 근거")
    return normalized


def attach_graph(document, entities, relations, context):
    warnings, good_entities, good_relations = [], [], []
    if not isinstance(entities, list):
        entities = []
        warnings.append("항목 구조를 사용할 수 없어 제외했습니다.")
    if not isinstance(relations, list):
        relations = []
        warnings.append("관계 구조를 사용할 수 없어 제외했습니다.")
    entity_counts = Counter(e.get("id") for e in entities if isinstance(e, dict) and isinstance(e.get("id"), str))
    relation_counts = Counter(e.get("id") for e in relations if isinstance(e, dict) and isinstance(e.get("id"), str))
    for raw in entities:
        try:
            entity = Entity.model_validate(raw).model_dump()
            if entity_counts[entity["id"]] != 1:
                raise ValueError("중복 항목 ID")
            entity["evidence"] = valid_evidence(entity["evidence"], document, context)
            good_entities.append(entity)
        except (ValidationError, ValueError, TypeError):
            warnings.append("잘못된 항목 1개를 그래프에서 제외했습니다.")
    allowed = {e["id"] for e in good_entities} | {f'{ref(d)}:{e["id"]}' for d in context for e in d["entities"]}
    for raw in relations:
        try:
            relation = Relation.model_validate(raw).model_dump()
            if relation_counts[relation["id"]] != 1 or relation["id"] in entity_counts:
                raise ValueError("중복 관계 ID")
            if relation["source"] not in allowed or relation["target"] not in allowed:
                raise ValueError("존재하지 않는 끝점")
            relation["evidence"] = valid_evidence(relation["evidence"], document, context)
            good_relations.append(relation)
        except (ValidationError, ValueError, TypeError):
            warnings.append("잘못된 관계 1개를 그래프에서 제외했습니다.")
    document.update(entities=good_entities, relations=good_relations, warnings=warnings,
                    graph_status=("partial" if good_entities else "unavailable") if warnings else "ready")
    return document


def document(raw, kind, context_version, context=(), parent=None, document_id=None, version=1):
    output = StoryOutput.model_validate(raw).model_dump()
    doc = {"document_id": document_id or new_id("doc"), "version": version,
           "schema_version": "1", "kind": kind, "decision": "pending", "context_version": context_version,
           "parent_document_ref": parent, **output}
    return attach_graph(doc, output["entities"], output["relations"], context)


def analyze_document(source, raw):
    output = AnalysisOutput.model_validate(raw).model_dump()
    doc = attach_graph(dict(source["document"]), output["entities"], output["relations"], [])
    for fact in output["facts"]:
        fact["evidence"] = valid_evidence(fact["evidence"], doc, [])
    if len({f["id"] for f in output["facts"]}) != len(output["facts"]):
        raise ValueError("사실 후보 ID가 중복됩니다")
    return doc, output["facts"]


def review(raw, doc, context):
    output = ReviewOutput.model_validate(raw).model_dump()
    ids = set()
    for issue in output["issues"]:
        if issue["id"] in ids:
            raise ValueError("쟁점 ID가 중복됩니다")
        ids.add(issue["id"])
        issue["status"] = "open"
        issue["evidence"] = valid_evidence(issue["evidence"], doc, context)
        locations = {(e["document_ref"], e["passage_id"]) for e in issue["evidence"]}
        if issue["kind"] == "conflict_candidate":
            if not any(r == ref(doc) for r, _ in locations) or not any(r != ref(doc) for r, _ in locations):
                raise ValueError("충돌 후보에는 기준과 초안의 근거가 필요합니다")
        if issue["kind"] == "internal_conflict" and len({p for r, p in locations if r == ref(doc)}) < 2:
            raise ValueError("내부 모순에는 초안 문단 두 곳이 필요합니다")
    return {"document_ref": ref(doc), **output}


def graph(run, view):
    selected = run[view]
    if not selected:
        raise ValueError("해당 문서가 아직 없습니다")
    documents = [*run["context"]["documents"], selected]
    issues = [i for r in run["reviews"] if r["document_ref"] == ref(selected) for i in r["issues"]]
    nodes, edges, warnings = [], [], []
    for doc in documents:
        warnings.extend(doc["warnings"])
        for key, output in (("entities", nodes), ("relations", edges)):
            for item in doc[key]:
                matches = [i["id"] for i in issues if any(e in i["evidence"] for e in item["evidence"])]
                data = {"id": f'{ref(doc)}:{item["id"]}', "label": item.get("name", item.get("label")),
                        "type": item["type"], "document_ref": ref(doc), "status": "input" if doc["kind"] == "input" else doc["decision"],
                        "kind": doc["kind"], "basis": item["basis"], "evidence": item["evidence"], "issue_ids": matches,
                        "description": item.get("description", ""), "condition": item.get("condition", "")}
                if key == "relations":
                    for endpoint in ("source", "target"):
                        data[endpoint] = item[endpoint] if ":" in item[endpoint] else f'{ref(doc)}:{item[endpoint]}'
                output.append({"data": data})
    return {"nodes": nodes, "edges": edges, "warnings": warnings}


def export_markdown(run):
    doc = run["revision"] or run["draft"]
    if not doc:
        raise ValueError("내보낼 문서가 없습니다")
    context = run["context"]["documents"]
    mapping = passage_map([*context, doc])
    external = {}
    generated_links = []
    def escape(value):
        return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("[", "\\[").replace("]", "\\]").replace("<", "&lt;").replace(">", "&gt;").replace("\n", " ")
    def link(e):
        key = (e["document_ref"], e["passage_id"])
        if key[0] == ref(doc):
            generated_links.append(f"passage-{key[1]}")
            return f'[{escape(key[1])}](#passage-{key[1]})'
        if key not in external:
            external[key] = len(external) + 1
        generated_links.append(f"evidence-{external[key]}")
        return f'[기준 {external[key]}](#evidence-{external[key]})'
    def endpoint(value):
        if ":" not in value:
            item = next(e for e in doc["entities"] if e["id"] == value)
            generated_links.append(f"entity-{value}")
            return f'[{escape(item["name"])}](#entity-{value})'
        dref, eid = value.rsplit(":", 1)
        source_doc = next(d for d in context if ref(d) == dref)
        item = next(e for e in source_doc["entities"] if e["id"] == eid)
        return f'{escape(item["name"])} ({", ".join(link(e) for e in item["evidence"])})'
    lines = [f'# {escape(doc["title"])}', "", f'문서: {ref(doc)} · {doc["kind"]} · {doc["decision"]}',
             f'기준 버전: {doc["context_version"]}', f'제공자: {run["provider"]}', f'실행 상태: {run.get("status", "unknown")}', ""]
    for p in doc["passages"]:
        lines += [f'## passage-{p["id"]}', "", p["text"].replace("<", "&lt;").replace(">", "&gt;"), ""]
    for item in doc["entities"]:
        lines += [f'## entity-{item["id"]}', "", f'{escape(item["name"])} · {item["type"]} · {item["basis"]}', escape(item["description"]),
                  "근거: " + ", ".join(link(e) for e in item["evidence"]), ""]
    lines += ["## 관계", "", "|ID|출발|의미|도착|조건|근거|", "|---|---|---|---|---|---|"]
    for r in doc["relations"]:
        lines.append(f'|{r["id"]}|{endpoint(r["source"])}|{escape(r["label"])}|{endpoint(r["target"])}|{escape(r["condition"])}|{", ".join(link(e) for e in r["evidence"])}|')
    lines += ["", "## 검토 결과", ""]
    for r in run["reviews"]:
        lines += [f'### 문서 {r["document_ref"]}', ""]
        for i in r["issues"]:
            # Previous-draft evidence is an appendix, never a dangling current-body anchor.
            lines += [f'- {escape(i["kind"])} · {i["status"]}: {escape(i["reason"])}', f'  제안: {escape(i["suggestion"])}']
            for e in i["evidence"]:
                if (e["document_ref"], e["passage_id"]) not in mapping and run["draft"] and e["document_ref"] == ref(run["draft"]):
                    mapping[(e["document_ref"], e["passage_id"])] = next(p["text"] for p in run["draft"]["passages"] if p["id"] == e["passage_id"])
            lines.append("  근거: " + ", ".join(link(e) for e in i["evidence"]))
    pending = merge_pending_issues(run.get("pending_issues", []), run["context"].get("unresolved_issues", []))
    shown = {(r["document_ref"], i["id"]) for r in run["reviews"] for i in r["issues"]}
    pending = [record for record in pending if (record["document_ref"], record["issue"]["id"]) not in shown]
    if pending:
        lines += ["", "## 보존한 미해결 쟁점", "", "아래 기록은 원래 검토 문서의 쟁점이며 현재 최종 문서의 검토 결과와 구분합니다. 채택이나 이어쓰기로 자동 해결되지 않습니다.", ""]
        for number, record in enumerate(pending, 1):
            issue = record["issue"]
            lines += [f'### 보존 쟁점 {number}', "", f'원래 실행: {escape(record["origin_run_id"])}',
                      f'원래 검토 문서: {escape(record["document_ref"])}',
                      f'{escape(issue["kind"])} · {escape(issue["status"])}: {escape(issue["reason"])}',
                      f'제안: {escape(issue["suggestion"])}', ""]
            for snapshot in record["evidence_snapshots"]:
                lines += [f'원래 근거: {escape(snapshot["document_ref"])} / {escape(snapshot["passage_id"])}',
                          "", escape(snapshot["text"]), ""]
    lines += ["", f'결정: {run["decision"]["status"]} — {escape(run["decision"]["reason"])}', ""]
    for (dref, pid), n in external.items():
        lines += [f'## evidence-{n}', "", f'{dref} / {pid}', "", escape(mapping[(dref, pid)]), ""]
    result = "\n".join(lines)
    anchors = set(re.findall(r'^## ([A-Za-z0-9_-]+)$', result, re.M))
    if any(anchor not in anchors for anchor in generated_links):
        raise ValueError("내보내기 내부 링크 검증 실패")
    return result
