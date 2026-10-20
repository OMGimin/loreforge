export interface Evidence {
  document_ref: string
  passage_id: string
}
export interface Passage { id: string; text: string }
export interface Entity {
  id: string
  type: string
  name: string
  description: string
  basis: string
  evidence: Evidence[]
}
export interface Relation {
  id: string
  source: string
  target: string
  type: string
  label: string
  condition: string
  basis: string
  evidence: Evidence[]
}
export interface StoryDocument {
  document_id: string
  version: string | number
  schema_version?: string
  kind: string
  decision: string
  context_version?: string
  parent_document_ref?: string | null
  graph_status: string
  warnings?: string[]
  title: string
  passages: Passage[]
  entities: Entity[]
  relations: Relation[]
}
export interface Fact {
  id: string
  text: string
  kind: string
  basis: string
  evidence: Evidence[]
}
export interface Usage {
  attempts: number
  retries: number
  input_tokens: number
  output_tokens: number
  elapsed_seconds: number
  calls: unknown[]
}
export interface Analysis {
  id: string
  source_id: string
  document: StoryDocument
  facts: Fact[]
  usage: Usage
  warnings: string[]
}
export interface Source {
  id: string
  text: string
  title: string
  document: StoryDocument
  analysis: Analysis | null
}
export interface Issue {
  id: string
  kind: string
  reason: string
  suggestion: string
  evidence: Evidence[]
  status: string
}
export interface Review { document_ref: string; issues: Issue[] }
export interface Run {
  id: string
  source_id: string
  analysis_id: string | null
  status: string
  stage: string
  request: string
  context: { version: string; documents: StoryDocument[]; constraints: string[] }
  draft: StoryDocument | null
  revision: StoryDocument | null
  reviews: Review[]
  decision: { status: string; reason: string }
  usage: Usage
  error: string | null
  warnings: string[]
  created_at: string
}
export interface GraphData {
  id: string
  label: string
  type: string
  document_ref: string
  status: string
  basis: string
  evidence: Evidence[]
  issue_ids: string[]
  source?: string
  target?: string
  description?: string
  condition?: string
}
export interface GraphItem { data: GraphData }
export interface GraphResponse { nodes: GraphItem[]; edges: GraphItem[]; warnings: string[] }
export interface Health {
  status: string
  provider: 'mock' | 'openai'
  key_configured: boolean
  models: { analyst: string; writer: string; reviewer: string }
}
export type DocumentView = 'input' | 'draft' | 'revision'
export const documentRef = (doc: StoryDocument) => `${doc.document_id}:${doc.version}`
export const kindLabel: Record<string, string> = { person: '인물', group: '세력', place: '장소', event: '사건', rule: '규칙' }
export const statusLabel: Record<string, string> = {
  input: '입력 자료', proposal: 'AI 제안', draft: 'AI 제안', revision: '수정 제안', pending: '미채택 제안',
  accepted: '채택한 설정', deferred: '보류한 제안', running: '진행 중', completed: '검토 완료',
  needs_attention: '확인 필요', failed: '실패', interrupted: '중단됨', resolved: '해결됨', open: '미해결',
}
export const basisLabel = (value: string) => value === 'explicit' ? '문서에 명시' : value === 'inferred' ? 'AI 추론' : value
export const issueLabel: Record<string, string> = {
  conflict_candidate: '기준과 충돌 후보', internal_conflict: '초안 내부 모순',
  missing_explanation: '설명 부족', intent_question: '의도 확인 질문',
}
