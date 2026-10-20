# 참고 자료와 근거 범위

확인 기준일: 2026-10-20. 각 자료의 주장을 실제 구현 결과나 전체 독자의 합의로 확대하지 않는다.

## 작품 사례

| 사례 | 확인한 근거 | 사용할 수 있는 설명 | 한계 |
|---|---|---|---|
| 죠죠의 기묘한 모험 | [4권 작가 후기 전사·번역](https://jojowiki.com/Volume_4#Afterword), [시저 설정 설명](https://jojowiki.com/Caesar_A._Zeppeli#Discrepancy) | 초기 원작의 가족관계 설정 충돌과 작가의 수정·사과 | 팬 아카이브에 전재된 자료. 실물 단행본을 확인하지 않았으며 현재 애니메이션에 같은 오류가 있다고 주장하지 않음 |
| 드래곤볼 Z | [CBR의 셀 재생 조건 비평](https://www.cbr.com/dragon-ball-z-cell-survived-goku-kamehameha-half-body/), [팬 토론](https://www.kanzenshuu.com/forum/viewtopic.php?t=26528) | 능력의 재생 조건과 앞선 장면의 일관성 논쟁 | 작가의 오류 인정이 아님. 반론과 더빙별 차이가 있음 |
| 노블레스 | [독자의 결말 질문](https://www.reddit.com/r/Noblesse/comments/ypfatd/what_happened_at_the_end/) | 생존 과정의 설명을 납득하지 못한 독자 반응 | 공개 검색 색인에서 확인한 토론. 원작 마지막 회차 직접 대조와 독자 전체 여론 조사는 하지 않음 |

발표의 대표 사례는 죠죠 한 개로 충분하다. 사례 수보다 문제의 유형과 서비스의 검토 기능을 연결하는 설명을 우선한다. 작품의 장면·이미지를 저장소에 수집하지 않는다.

## 실제 적용 자료

사용자가 지정한 Google Drive의 **「강호행 스토리 3차 수정본」**을 뿌리 스토리로 사용한다. 원문 본문을 읽고 사용자 지정 기준본임을 확인했다. 파일의 수정일이 다른 참고 문서보다 이르더라도 임의로 기준본을 바꾸지 않는다.

원문 URL·구체적인 검토 후보는 Git에서 제외한 로컬 자료에 둔다. 1차·2차 수정본, 별도의 무협 설정집은 이번 기획에서 통합하지 않았다. 본 프로젝트의 기획서가 기존 강호행 설정집을 대체하지 않는다.

## 수업 자료

- 「AI 서비스 Mini-Project.pdf」, 뷰어 기준 3쪽: Day 1 계획 등록, Day 3 14시 구현·결과보고, 17시30분 발표 일정.
- 10쪽: Base 개선 또는 신규 프로젝트, 주제·AI 도구 선택 허용, 자신의 코드와 내용 설명 필요.
- 11~12쪽: RAG·모듈화·서브그래프·사람 승인·평가 확장 등의 예시. 전부 필수라고 해석하지 않음.
- 13쪽: 기능 완성도 30, 기술 고려 30, 한계·트레이드오프 20, 발표 20. 같은 입력의 Before/After 실측 비교 10은 보너스.
- 제공된 `all_total_vue_student`: 서버, 역할별 백엔드, LangGraph, MCP, SQLite, SSE, Vue 화면의 참고 코드. 구현 시 실제 가져온 코드와 변경 범위를 README에 기록한다. 수업 코드의 재배포 허용 범위는 별도 확인한다.

과제 등록 양식이나 외부 제출 링크를 이 문서 작성만으로 조작하지 않는다. 위 일정과 평가는 제공 PDF의 내용이며 최종 제출 완료를 뜻하지 않는다.

## 기술 자료

- [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses): 스키마를 따르는 출력과 JSON 모드의 차이, 거절·불완전 응답 처리 참고. 구조가 맞는다고 내용의 정확성이 보장되는 것은 아니다.
- [Munder Difflin 저장소](https://github.com/HarnessMD/munder-difflin): 사용자가 기존 실행형 AI를 역할별로 조율하는 도구의 예로 제시한 참고. LoreForge MVP는 사용자가 보유한 OpenAI API 키로 시작하며 CLI 구독 연동은 후속 후보.

API 모델·패키지 버전은 구현 시 실제 가용성과 호환성을 확인하고 고정한다. 이 문서에 모델 최신성·요금·실제 연결 성공을 가정해 적지 않는다.
