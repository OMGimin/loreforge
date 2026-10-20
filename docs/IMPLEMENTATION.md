# LoreForge 구현 기록

## 구현 목표

키워드 입력부터 생성·검토·최대 1회 수정·그래프 탐색·채택·이어쓰기까지 연결한다. Vue 3/TypeScript/Vite/Cytoscape.js와 FastAPI/Pydantic/SQLite/LangGraph/OpenAI Responses를 사용한다. 공개용 가상 데이터로 모의 검증과 실제 API 검증을 구분한다. 별도 개인 원고 검증은 공통 기능의 이해·평가용이며 공개 fixtures와 분리한다.

## 작업 분담

- 백엔드: `backend/`, `tests/`, `pyproject.toml`. 저장·AI 루프·MCP·API·서버 테스트.
- 프런트엔드: `frontend/`. 어두운 작업실·본문/그래프·근거·채택·이력·복원.
- 통합: 실행 스크립트·문서·환경 예시·통합 및 브라우저 검증. 검증 후 로컬 커밋, 원격 전송 제외.

## 공통 API 계약

개발 시 프런트엔드는 `/api`를 FastAPI의 동일 경로로 프록시한다. JSON 필드는 snake_case다. 모든 오류는 HTTP 오류 코드와 `{detail: string}`을 반환한다. 시간은 ISO 8601 UTC다.

- `GET /api/health`: `{status, provider: "openai"|"mock", key_configured, models: {analyst, writer, reviewer}}`. 키 값은 반환하지 않는다. 모의 모드는 화면에 항상 표시한다.
- `POST /api/sources`: `{text, title?}` → `{id, text, title, document, analysis: null}`. UTF-8 TXT/MD는 브라우저가 읽어 text로 보낸다. 파일 100KB, text 20,000자, title 200자.
- `GET /api/sources/{id}`: 같은 구조. 분석 후 analysis와 document의 entities/relations가 채워진다.
- `POST /api/sources/{id}/analyze`: `{request_id, trace: false}` → `{id, source_id, document, facts, usage, warnings}`. 분석은 비동기 HTTP 응답을 기다린다. 동일 request_id 재요청은 저장 결과를 반환하며 중복 호출하지 않는다. 실행 전 상태와 중단은 DB에 기록한다.
- `POST /api/runs`: `{request_id, analysis_id?: string, accepted_run_id?: string, constraints: string[], request: string, trace: false}` → Run. 둘 중 하나만 지정. 분석 ID의 사용량을 포함하되 비용 중복 합산하지 않는다. 새 실행은 즉시 running으로 반환하고 서버 작업으로 진행한다. 분석 하나는 한 실행에만 소비하며 중복 제출은 같은 run을 반환한다.
- `GET /api/runs`: `{runs: Run[]}` (최근 순).
- `GET /api/runs/{id}`: Run 전체.
- `GET /api/runs/{id}/events`: SSE `event: progress`, `data: {id, run_id, stage, message, status}`. 이벤트 ID/Last-Event-ID로 재연결하며 실행하지 않는다. 마지막 이벤트 후 클라이언트는 GET으로 결과를 읽는다.
- `GET /api/runs/{id}/graph?view=draft|revision`: `{nodes: GraphItem[], edges: GraphItem[], warnings: string[]}`. GraphItem은 Cytoscape 형식 `{data: {id, label, type, document_ref, status, basis, evidence, issue_ids, source?, target?, description?, condition?}}`이다. evidence는 document_ref와 passage_id이며 self를 전체 참조로 해석한다. 표시 한도는 클라이언트가 적용하고 전체 데이터는 보존한다.
- `POST /api/runs/{id}/decision`: `{status: "accepted"|"deferred", reason: string}` → 갱신 Run. 최종 문서에만 적용한다.
- `GET /api/runs/{id}/export`: text/markdown 다운로드.

Run 최소 필드: `id, source_id, analysis_id, status, stage, request, context: {version, documents: StoryDocument[], constraints: string[]}, draft: StoryDocument|null, revision: StoryDocument|null, reviews: Review[], decision: {status, reason}, usage: {attempts, retries, input_tokens, output_tokens, elapsed_seconds, calls: []}, error: string|null, warnings: string[], created_at`.

Run·analysis와 각 호출에 `prompt_version`을 기록하고 호출에는 요청 모델과 `response_model`을 구분한다. 실패 응답의 확인된 토큰도 합산하며, 응답을 받지 못한 호출은 `usage_known: false`와 경고로 남긴다.

Review: `{document_ref, issues: Issue[]}`. Issue: `{id, kind, reason, suggestion, evidence: Evidence[], status: "open"|"resolved"}`. kind는 conflict_candidate/internal_conflict/missing_explanation/intent_question. 현재 문서의 검토와 과거 미해결 쟁점을 구분한다. `pending_issues`에는 원래 문서·근거 스냅샷을 보존하며, 의도 질문을 수정·채택·이어쓰기로 자동 해결하지 않는다. 이어받은 쟁점은 `context.unresolved_issues`로 전달하고 이전 초안을 확정 기준 문서로 편입하지 않는다. 사실 후보 Fact: `{id, text, kind, basis, evidence}`.

StoryDocument는 SETTING_DOCUMENT_FORMAT.md를 따른다. 서버 필드: `document_id, version, schema_version, kind, decision, context_version, parent_document_ref, graph_status, warnings`. 본문 필드: `title, passages, entities, relations`. 문서 참조는 `document_id:version`, 항목 참조는 여기에 `:entity_id`를 붙인다.

## 구현 정책

분석/창작/검토 모델은 각각 OPENAI_ANALYST_MODEL=gpt-5.4-mini, OPENAI_WRITER_MODEL=gpt-5.4, OPENAI_REVIEWER_MODEL=gpt-5.4. OPENAI_API_KEY는 서버에서만 읽는다. LORE_PROVIDER=mock은 테스트·명시적 모의 시연 전용이며 API 키 부재의 자동 대체가 아니다. 데이터 경로 LORE_DB_PATH 기본 runtime/loreforge.db. LangSmith는 실행별 trace opt-in이며 기본 off다. 개인 확인에 사용할 기존 설정집 개인 검증은 분석·생성 모두 `trace=False`로 실행하고 원문·생성물·DB·평가 원문을 ignored `runtime/`에만 저장한다. 작품별 기능·프롬프트 변경은 하지 않으며 공개 fixtures를 유지한다. 본문·요청·프롬프트는 전송하지 않고 작업·모델·분기·MCP·토큰 등 메타데이터만 전송한다.

정상 5회/재시도 포함 6회, 자동 수정 1회, SDK 재시도 0, 429/일시적 5xx 전체 1회 재시도. 호출 120초/자동 실행 600초. 기준 20,000자/요청 2,000자. raw 입력·조건·채택 문서만 기준에 포함한다. 단일 프로세스에서 동시 AI 작업 1개. 서버 재시작 시 running은 interrupted 처리하고 재개하지 않는다.

## 검증 기록

로컬 MVP 구현 뒤 자동·타입·빌드 검사, 실제 MCP stdio, 모의 브라우저 시나리오, 공개 입력의 실제 첫 생성·채택 후 이어쓰기와 별도 폴더 설치를 확인했다. 최초 구현의 자동 검사 31개는 초기 기록이며 추가 회귀·검증 도구 테스트의 최신 수는 [구현 결과](RESULTS.md)에 기록한다. 새 검토 사례 6건은 v3·v4에서 각각 기대 판단과 일치했다.

LangSmith는 원격 API에서 분석·생성 root 2개와 총 10개 span을 조회해 모델·토큰·MCP·수정 분기·최종 상태를 대조했다. 웹 화면 로그인 후 표시는 미확인이다. 개인 원고 검증은 실제 생성·검토·1회 수정·재검토를 실행했고 남은 지적 2건을 표시했다. 사람 평가·채택은 미확인이다. 발표자료·5분 리허설은 현재 필수 산출물에서 제외하고 실행·설계·검증·한계 문서는 유지한다.
