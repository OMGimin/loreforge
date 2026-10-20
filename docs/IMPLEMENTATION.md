# LoreForge 구현 기록

## 구현 목표

‘새 이야기 만들기’는 키워드 입력부터 생성·검토·최대 1회 수정·그래프 탐색·채택·이어쓰기까지 연결한다. ‘기존 설정 검토’는 같은 원문 등록·분석 구조를 사용하되 원고 검토 1회와 쟁점 확인·보고서 내보내기로 끝낸다. 검토 전용 흐름과 그래프 표시 범위는 별도 작업 트리에 구현했고 현재 코드의 자동·타입·빌드 검사와 공개 원고의 실제 API 검토·근거 이동을 확인했다. 아래에서 현재 증거와 과거 검증 기록을 구분한다. 기존 main에는 병합·푸시하지 않았다. Vue 3/TypeScript/Vite/Cytoscape.js와 FastAPI/Pydantic/SQLite/LangGraph/OpenAI Responses를 사용한다. 공개용 가상 데이터로 모의 검증과 실제 API 검증을 구분한다. 별도 개인 원고 검증은 공통 기능의 이해·평가용이며 공개 fixtures와 분리한다.

## 작업 분담

- 백엔드: `backend/`, `tests/`, `pyproject.toml`. 저장·AI 루프·MCP·API·서버 테스트.
- 프런트엔드: `frontend/`. 네이비 작업 패널·밝은 본문/그래프·근거·채택·이력·복원.
- 통합: 실행 스크립트·문서·환경 예시·통합 및 브라우저 검증. 검증 후 로컬 커밋, 원격 전송 제외.

## 공통 API 계약

개발 시 프런트엔드는 `/api`를 FastAPI의 동일 경로로 프록시한다. JSON 필드는 snake_case다. 모든 오류는 HTTP 오류 코드와 `{detail: string}`을 반환한다. 시간은 ISO 8601 UTC다.

- `GET /api/health`: `{status, provider: "openai"|"mock", key_configured, models: {analyst, writer, reviewer}}`. 키 값은 반환하지 않는다. 모의 모드는 화면에 항상 표시한다.
- `POST /api/sources`: `{text, title?}` → `{id, text, title, document, analysis: null}`. UTF-8 TXT/MD는 브라우저가 읽어 text로 보낸다. 파일 100KB, text 20,000자, title 200자.
- `GET /api/sources/{id}`: 같은 구조. 분석 후 analysis와 document의 entities/relations가 채워진다.
- `POST /api/sources/{id}/analyze`: `{request_id, trace: false}` → `{id, source_id, document, facts, usage, warnings}`. 분석은 비동기 HTTP 응답을 기다린다. 동일 request_id 재요청은 저장 결과를 반환하며 중복 호출하지 않는다. 실행 전 상태와 중단은 DB에 기록한다.
- `POST /api/runs`: `{request_id, mode?: "create"|"review", analysis_id?: string, accepted_run_id?: string, constraints: string[], request: string, trace: false}` → Run. 둘 중 하나만 지정. mode 생략 시 create이며 review는 analysis_id만 받는다. 분석 ID의 사용량을 포함하되 비용 중복 합산하지 않는다. 새 실행은 즉시 running으로 반환하고 서버 작업으로 진행한다. 분석 하나는 한 실행에만 소비하며 중복 제출은 같은 run을 반환한다.
- `GET /api/runs`: `{runs: Run[]}` (최근 순).
- `GET /api/runs/{id}`: Run 전체.
- `GET /api/runs/{id}/events`: SSE `event: progress`, `data: {id, run_id, stage, message, status}`. 이벤트 ID/Last-Event-ID로 재연결하며 실행하지 않는다. 마지막 이벤트 후 클라이언트는 GET으로 결과를 읽는다.
- `GET /api/runs/{id}/graph?view=draft|revision|input`: `{nodes: GraphItem[], edges: GraphItem[], warnings: string[]}`. 검토 실행은 input 또는 기본 draft 뷰를 review_document로 해석한다. 전체 응답 중 화면에는 현재 문서와 참조 기준 끝점을 기본 표시하고 기준 문맥은 선택적으로 펼친다. 이름이 같아도 문서별 ID를 유지한다. GraphItem은 Cytoscape 형식 `{data: {id, label, type, document_ref, status, basis, evidence, issue_ids, source?, target?, description?, condition?}}`이다. evidence는 document_ref와 passage_id이며 self를 전체 참조로 해석한다. 표시 한도는 클라이언트가 적용하고 전체 데이터는 보존한다.
- `POST /api/runs/{id}/decision`: `{status: "accepted"|"deferred", reason: string}` → 갱신 Run. 생성 실행의 최종 문서에만 적용한다. 검토 실행에는 결정과 이어쓰기를 허용하지 않는다.
- `GET /api/runs/{id}/export`: 생성 결과 또는 원문·근거·쟁점·수정 방향을 담은 검토 보고서의 text/markdown 다운로드.

Run 최소 필드: `id, mode, source_id, analysis_id, status, stage, request, context: {version, documents: StoryDocument[], constraints: string[]}, draft: StoryDocument|null, revision: StoryDocument|null, review_document: StoryDocument|null, reviews: Review[], decision: {status, reason}, usage: {attempts, retries, input_tokens, output_tokens, elapsed_seconds, calls: []}, error: string|null, warnings: string[], created_at`.

검토 실행은 mode와 검토 대상 원고 스냅샷 review_document를 저장하고 draft·revision은 null로 둔다. 원문·버전·문단 ID를 유지하며 창작 담당·자동 수정·자동 채택을 실행하지 않는다. 검토 결과의 쟁점 유무에 따라 completed 또는 needs_attention으로 끝나며 쟁점 없음은 정확성 보증이 아니다. 원고·관련 조건 인용, 유형·이유·수정 방향과 문단 이동을 화면에서 먼저 보여준다.

Run·analysis와 각 호출에 `prompt_version`을 기록하고 호출에는 요청 모델과 `response_model`을 구분한다. 실패 응답의 확인된 토큰도 합산하며, 응답을 받지 못한 호출은 `usage_known: false`와 경고로 남긴다.

Review: `{document_ref, issues: Issue[], screening?: ReviewCandidate[]}`. `ReviewCandidate`는 `{issue, verdict: unresolved|explained|optional, counter_evidence: Evidence[], rationale}`다. OpenAI 검토 응답은 `candidates`를 반환하고 서버가 문서·문단 참조를 검증해 unresolved만 issues에 넣는다. explained는 실제 해소 근거가 필수이며 존재하지 않는 참조·중복 후보 ID는 검토 실패다. 기존 저장 결과와 모의 응답의 issues 계약은 읽기 호환성을 유지한다. 제외 판정·근거는 원문 검토 화면과 Markdown 보고서에서 확인한다. 의미 판단은 같은 모델의 판정이며 참조 검증만으로 정확성을 보장하지 않는다. Issue: `{id, kind, reason, suggestion, evidence: Evidence[], status: "open"|"resolved"}`. kind는 conflict_candidate/internal_conflict/missing_explanation/intent_question. 현재 문서의 검토와 과거 미해결 쟁점을 구분한다. `pending_issues`에는 원래 문서·근거 스냅샷을 보존하며, 의도 질문을 수정·채택·이어쓰기로 자동 해결하지 않는다. 이어받은 쟁점은 `context.unresolved_issues`로 전달하고 이전 초안을 확정 기준 문서로 편입하지 않는다. 사실 후보 Fact: `{id, text, kind, basis, evidence}`.

StoryDocument는 SETTING_DOCUMENT_FORMAT.md를 따른다. 서버 필드: `document_id, version, schema_version, kind, decision, context_version, parent_document_ref, graph_status, warnings`. 본문 필드: `title, passages, entities, relations`. 문서 참조는 `document_id:version`, 항목 참조는 여기에 `:entity_id`를 붙인다.

## 구현 정책

분석/창작/검토 모델은 각각 OPENAI_ANALYST_MODEL=gpt-5.4-mini, OPENAI_WRITER_MODEL=gpt-5.4, OPENAI_REVIEWER_MODEL=gpt-5.4. OPENAI_API_KEY는 서버에서만 읽는다. LORE_PROVIDER=mock은 테스트·명시적 모의 시연 전용이며 API 키 부재의 자동 대체가 아니다. 데이터 경로 LORE_DB_PATH 기본 runtime/loreforge.db. LangSmith는 실행별 trace opt-in이며 기본 off다. 개인 확인에 사용할 기존 설정집 개인 검증은 분석·생성 모두 `trace=False`로 실행하고 원문·생성물·DB·평가 원문을 ignored `runtime/`에만 저장한다. 작품별 기능·프롬프트 변경은 하지 않으며 공개 fixtures를 유지한다. LangSmith에는 본문·요청·프롬프트를 전송하지 않고 작업·모델·분기·MCP·토큰 등 메타데이터만 전송한다. 분석·생성·검토에 필요한 원문은 OpenAI API로 전송되며 입력 화면에서 알린다.

생성은 정상 최대 5회/재시도 포함 6회, 자동 수정 1회다. 검토 전용은 분석+검토 정상 2회/재시도 포함 최대 3회다. SDK 재시도 0, 분석과 실행에 걸쳐 429/일시적 5xx 재시도를 전체 1회만 공유한다. 기존 분석 멱등성·단일 작업 게이트·SSE·예산 저장과 재시작 복원을 유지한다. 호출 120초/자동 실행 600초. 기준 20,000자/요청 2,000자. raw 입력·조건·채택 문서만 기준에 포함한다. 단일 프로세스에서 동시 AI 작업 1개. 서버 재시작 시 running은 interrupted 처리하고 재개하지 않는다.

그래프는 COSE 연결 기반 배치를 기본으로 하고, 선택 중심에서는 연결 거리별 동심원으로 펼친다. 이름 경계를 고려하고 연결 없는 항목을 아래쪽에 따로 모은다. 크게 보기는 같은 Cytoscape DOM을 native dialog로 이동해 선택·필터를 보존하며 닫기/Escape 뒤 원래 버튼에 초점이 돌아간다. 확대에서는 화면 비율에 맞춰 연결 묶음 방향과 캔버스 높이를 조정한다. 그래프 관계 이름은 선택·마우스 올림 또는 전체 표시 선택 때만 드러낸다. 화면 맞춤은 라벨을 포함하고 크기 변경에 대응하며 최소 배율을 낮춰 큰 그래프를 담는다. 조작 도구는 캔버스 밖에 둔다.

## 화면 색상과 본문 가독성

초록·골드 테마를 네이비(`#000926`)·아이스 블루(`#D6E6F3`)·파우더 블루(`#A6C5D7`)·사파이어(`#0F52BA`) 계열로 변경했다. 읽기 영역은 `#F8FAFC`, 본문 글자는 `#172B45`를 사용한다. 본문은 데스크톱 18px/좁은 화면 17px, 줄 간격 1.85, 최대 문서 폭 800px이며 문단 간 여백과 근거 강조를 넓혔다. 검토 설명 16px, 인용문 14px로 본문과 보조 정보를 구분한다. 경고·오류는 별도 의미 색과 기존 상태 문구를 유지한다.

Cytoscape 노드·연결선·라벨 배경도 밝은 블루 테마로 맞췄다. 종류별 도형은 유지하고 선택 항목은 사파이어, 쟁점 근거는 주황색으로 구분한다. 데이터·문서 내용·모델 실행 흐름은 변경하지 않는다.

색상 변경 후 `npm run build`(타입 검사 포함), `npm test` 11개가 통과했다. 실제 5176 화면의 검토 결과·본문·확대 관계도를 확인하고, 그래프 항목 선택→근거 문단 이동(`passage-p001`)을 확인했다. 1000px 브라우저 너비에서 가로 오버플로가 없고 본문 계산 스타일이 18px/33.3px임을 확인했다. 이번 검증은 저장된 결과 탐색이며 새로운 모델 호출은 실행하지 않았다. 모바일 기기 실측과 별도 사용자 가독성 평가는 미실시다.

## 검증 기록

검토 전용 추가 시점의 scripts/check.sh는 Python 검사 98개·프런트엔드 검사 7개·타입 검사·빌드가 통과했다. 이후 과잉 지적과 연결 기반 배치 보완의 최신 검사는 RESULTS.md에 기록한다. 공개 가상 원고의 실제 HTTP/API 검토 2건은 각각 분석+검토 2회로 종료했고 원문 불변·창작/자동 수정 미호출·실제 MCP 조회·Markdown 내보내기를 확인했다. 브라우저에서 쟁점 우선 표시→근거 이동→그래프→선택 항목의 관련 쟁점 연결을 확인했다. 현재 문서·기준 함께 보기의 구분, 30항목·60관계 표시와 한도 밖 검색·빈 결과, 저장 결과 새로고침 복원을 확인했다. 상세 실행 증거와 한계는 [구현 결과](RESULTS.md)에 기록한다.

현재 검토 프롬프트는 `loreforge-v7-counterevidence`다. 원문 전체의 설명·예외·배타적 분기를 먼저 확인하고 해소된 지적·선택적 상세화를 제외한다. 후보는 최대 12개이며 별도 모델 호출을 추가하지 않는다. v6에서 도입한 근거 규칙을 유지해, 한 문단 내부에 모순되는 두 주장이 있으면 실제 원문 근거 1개와 두 주장을 설명한 이유를 허용한다. 근거 중복은 거절하며 생성 초안 검토의 두 문단 근거 조건은 유지한다.

아래는 검토 전용 추가 이전 생성 MVP의 검증 기록이다.

로컬 MVP 구현 뒤 자동·타입·빌드 검사, 실제 MCP stdio, 모의 브라우저 시나리오, 공개 입력의 실제 첫 생성·채택 후 이어쓰기와 별도 폴더 설치를 확인했다. 최초 구현의 자동 검사 31개는 초기 기록이며 추가 회귀·검증 도구 테스트의 최신 수는 [구현 결과](RESULTS.md)에 기록한다. 새 검토 사례 6건은 v3·v4에서 각각 기대 판단과 일치했다.

LangSmith는 원격 API에서 분석·생성 root 2개와 총 10개 span을 조회해 모델·토큰·MCP·수정 분기·최종 상태를 대조했다. 웹 화면 로그인 후 표시는 미확인이다. 개인 원고 검증은 실제 생성·검토·1회 수정·재검토를 실행했고 남은 지적 2건을 표시했다. 작성자의 구조적 유용성·설명문 형태 유지 선호는 확인했다. 작성자는 원래 설정 의도가 대체로 유지됐다고 평가했으며 구체적인 선택지는 열린 채로 두기로 했다. 전체 채택과는 구분한다. 발표자료·5분 리허설은 현재 필수 산출물에서 제외하고 실행·설계·검증·한계 문서는 유지한다.
