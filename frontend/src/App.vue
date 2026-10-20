<script setup lang="ts">
import { computed, defineAsyncComponent, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api, recall, remember, requestId } from './api'
import type { Analysis, DocumentView, Evidence, GraphData, GraphResponse, Health, Issue, Run, Source, StoryDocument } from './types'
import { basisLabel, documentRef, evidenceLabel, issueLabel, kindLabel, passageLabel, statusLabel } from './types'

const RelationshipGraph = defineAsyncComponent(() => import('./components/RelationshipGraph.vue'))

const health = ref<Health | null>(null)
const sourceText = ref('')
const sourceTitle = ref('')
const source = ref<Source | null>(null)
const analysis = ref<Analysis | null>(null)
const selectedFacts = ref<string[]>([])
const directConstraints = ref('')
const request = ref('')
const trace = ref(false)
const run = ref<Run | null>(null)
const history = ref<Run[]>([])
const continuing = ref<Run | null>(null)
const currentView = ref<DocumentView>('input')
const mode = ref<'body' | 'graph'>('body')
const mobilePanel = ref<'materials' | 'document' | 'review'>('materials')
const selectedItem = ref<GraphData | null>(null)
const selectedIssue = ref<Issue | null>(null)
const evidenceDocument = ref<StoryDocument | null>(null)
const highlightedPassage = ref('')
const decisionReason = ref('')
const error = ref('')
const feedback = ref('')
const loading = ref(true)
const analyzing = ref(false)
const submitting = ref(false)
const deciding = ref(false)
const graphLoading = ref(false)
const graphError = ref('')
const graph = ref<GraphResponse>({ nodes: [], edges: [], warnings: [] })
const progress = ref<{ id: string; stage: string; message: string }[]>([])
const fileInput = ref<HTMLInputElement>()
const graphCache = new Map<string, GraphResponse>()
let restoring = false
let analyzeRequest = requestId()
let generationRequest = requestId()
let events: EventSource | undefined
let polling: ReturnType<typeof setInterval> | undefined
let loadSequence = 0
let graphSequence = 0
let refreshSequence = 0

const running = computed(() => run.value?.status === 'running')
const busy = computed(() => loading.value || analyzing.value || submitting.value || running.value)
const locked = computed(() => Boolean(run.value || continuing.value) || busy.value)
const providerReady = computed(() => Boolean(health.value && (health.value.provider === 'mock' || health.value.key_configured)))
const constraints = computed(() => [
  ...(analysis.value?.facts.filter(fact => selectedFacts.value.includes(fact.id)).map(fact => fact.text) || []),
  ...directConstraints.value.split('\n').map(line => line.trim()).filter(Boolean),
])
const continuationDocuments = computed(() => {
  if (!continuing.value) return []
  const final = continuing.value.revision || continuing.value.draft
  const documents = [...continuing.value.context.documents]
  if (final && !documents.some(doc => documentRef(doc) === documentRef(final))) documents.push(final)
  return documents
})
const contextLength = computed(() => {
  const original = continuing.value
    ? continuationDocuments.value.reduce((sum, doc) => sum + doc.passages.reduce((count, p) => count + p.text.length, 0), 0)
    : sourceText.value.length
  return original + constraints.value.join('\n').length
})
const inputError = computed(() => {
  if (!sourceText.value.trim() && !continuing.value) return '키워드 몇 개나 짧은 메모만으로도 시작할 수 있어요.'
  if (contextLength.value > 20000) return '재료·보호 조건·이어받는 설정을 합해 20,000자 이내로 줄여주세요.'
  if (sourceTitle.value.length > 200) return '제목은 200자 이내로 줄여주세요.'
  if (request.value.length > 2000) return '확장 요청은 2,000자 이내로 줄여주세요.'
  return ''
})
const canAnalyze = computed(() => !busy.value && !run.value && !continuing.value && !inputError.value && providerReady.value)
const canGenerate = computed(() => !busy.value && !run.value && Boolean(analysis.value || continuing.value) && !inputError.value && providerReady.value)
const selectedDocument = computed(() => currentView.value === 'input' ? source.value?.document || null : run.value?.[currentView.value] || null)
const bodyDocument = computed(() => evidenceDocument.value || selectedDocument.value)
const viewLabels: Record<DocumentView, string> = { input: '입력 자료', draft: '첫 초안', revision: '수정본' }
const currentIssues = computed(() => {
  if (!run.value || currentView.value === 'input' || !selectedDocument.value) return []
  return run.value.reviews.filter(review => review.document_ref === documentRef(selectedDocument.value!)).flatMap(review => review.issues)
})
const finalDocument = computed(() => run.value?.revision || run.value?.draft || null)
const finalView = computed(() => finalDocument.value && selectedDocument.value && documentRef(finalDocument.value) === documentRef(selectedDocument.value))
const decisionAllowed = computed(() => Boolean(run.value && ['completed', 'needs_attention'].includes(run.value.status) && run.value.reviews.length && finalDocument.value))
const allDocuments = computed(() => {
  const documents = [source.value?.document, ...(run.value?.context.documents || []), run.value?.draft, run.value?.revision, ...continuationDocuments.value]
  return documents.filter((doc): doc is StoryDocument => Boolean(doc)).filter((doc, i, docs) => docs.findIndex(other => documentRef(other) === documentRef(doc)) === i)
})
const usage = computed(() => run.value?.usage || analysis.value?.usage)
const runWarnings = computed(() => {
  const counts = new Map<string, number>()
  for (const warning of run.value?.warnings || []) counts.set(warning, (counts.get(warning) || 0) + 1)
  return [...counts].map(([message, count]) => ({ message, count }))
})
const unresolved = computed(() => currentIssues.value.filter(issue => issue.status !== 'resolved').length)
const pendingIssues = computed(() => (run.value?.pending_issues || run.value?.context.unresolved_issues || []).filter(entry =>
  !selectedDocument.value || entry.document_ref !== documentRef(selectedDocument.value) ||
  !currentIssues.value.some(issue => issue.id === entry.issue.id),
))
const stageNames: Record<string, string> = {
  preparing: '기준 준비', analyzing: '재료 분석', analysis: '재료 분석', queued: '작업 준비',
  draft: '초안 작성', writing: '초안 작성', generating: '초안 작성', review: '근거 검토', reviewing: '근거 검토',
  revision: '한 번 수정', revising: '한 번 수정', rereview: '수정본 재검토', reviewing_revision: '수정본 재검토',
  completed: '검토 완료', needs_attention: '확인 필요', failed: '실행 실패', interrupted: '실행 중단',
  analyze: '재료 분석', generate: '초안 작성', generated: '초안 저장', revise: '한 번 수정', revised: '수정본 저장',
  review_revision: '수정본 재검토', reviewed: '검토 저장', finished: '검토 종료',
}
const stageText = computed(() => analyzing.value ? '입력 재료를 정리하고 있어요' : stageNames[run.value?.stage || ''] || run.value?.stage || '준비되었습니다')
const disableReason = computed(() => {
  if (loading.value) return '서버와 저장된 작업을 확인하고 있습니다.'
  if (!health.value) return '서버 연결을 먼저 확인해주세요.'
  if (!providerReady.value) return '서버에 OpenAI API 키가 설정되지 않았습니다.'
  if (analyzing.value) return '재료 분석을 기다려주세요. 같은 요청을 다시 만들지 않습니다.'
  if (running.value || submitting.value) return '현재 작업이 진행 중입니다.'
  return inputError.value
})

async function focusPanel(panel: 'materials' | 'document' | 'review', target: string) {
  mobilePanel.value = panel
  await nextTick()
  const element = document.getElementById(target)
  element?.focus({ preventScroll: true })
  element?.scrollIntoView({ behavior: 'smooth', block: 'center' })
}
function showFinalDocument() {
  currentView.value = run.value?.revision ? 'revision' : 'draft'
  mode.value = 'body'
  evidenceDocument.value = null
  highlightedPassage.value = ''
  mobilePanel.value = 'document'
}
function resetSelection() {
  selectedItem.value = null
  selectedIssue.value = null
  evidenceDocument.value = null
  highlightedPassage.value = ''
}
function applySource(value: Source) {
  restoring = true
  source.value = value
  sourceText.value = value.text
  sourceTitle.value = value.title || ''
  analysis.value = value.analysis
  remember('source', value.id)
  restoring = false
}
watch([sourceText, sourceTitle], () => {
  if (restoring) return
  source.value = null
  analysis.value = null
  selectedFacts.value = []
  analyzeRequest = requestId()
  generationRequest = requestId()
  remember('source', null)
}, { flush: 'sync' })
watch(currentView, () => { resetSelection(); void loadGraph() })

function stopWatching() {
  ++refreshSequence
  events?.close()
  events = undefined
  if (polling) clearInterval(polling)
  polling = undefined
}
function updateRun(value: Run, initial = false) {
  const previous = run.value
  run.value = value
  remember('run', value.id)
  decisionReason.value = value.decision.reason || ''
  if (initial || (value.revision && !previous?.revision)) currentView.value = value.revision ? 'revision' : value.draft ? 'draft' : 'input'
  else if (value.draft && !previous?.draft && currentView.value === 'input') currentView.value = 'draft'
  history.value = [value, ...history.value.filter(item => item.id !== value.id)]
  if (value.status !== 'running') {
    stopWatching()
    void loadGraph()
  }
}
async function refreshRun(id: string) {
  const sequence = ++refreshSequence
  try {
    const value = await api<Run>(`/runs/${encodeURIComponent(id)}`)
    if (sequence !== refreshSequence || run.value?.id !== id || deciding.value) return
    if (run.value.status !== 'running' && value.status === 'running') return
    updateRun(value)
  } catch { /* 재연결 중에도 마지막으로 확인한 본문은 유지한다. */ }
}
function watchRun(id: string) {
  stopWatching()
  events = new EventSource(`/api/runs/${encodeURIComponent(id)}/events`)
  events.addEventListener('progress', event => {
    if (run.value?.id !== id || run.value.status !== 'running') return
    try {
      const message = JSON.parse((event as MessageEvent).data) as { id: string; stage: string; message: string; status: string }
      if (!progress.value.some(item => item.id === message.id)) progress.value.push(message)
      progress.value = progress.value.slice(-8)
      if (run.value) run.value.stage = message.stage
      void refreshRun(id)
    } catch { feedback.value = '진행 상태를 다시 확인하고 있습니다. 저장된 결과는 계속 조회할 수 있습니다.' }
  })
  events.onerror = () => { if (run.value?.status === 'running') void refreshRun(id) }
  polling = setInterval(() => { if (run.value?.id === id) void refreshRun(id) }, 3000)
}
async function openRun(id: string) {
  if (analyzing.value || submitting.value) return
  const sequence = ++loadSequence
  loading.value = true
  error.value = ''
  resetSelection()
  stopWatching()
  try {
    const value = await api<Run>(`/runs/${encodeURIComponent(id)}`)
    const savedSource = await api<Source>(`/sources/${encodeURIComponent(value.source_id)}`)
    if (sequence !== loadSequence) return
    continuing.value = null
    applySource(savedSource)
    request.value = value.request
    directConstraints.value = value.context.constraints.join('\n')
    selectedFacts.value = []
    progress.value = []
    updateRun(value, true)
    mobilePanel.value = 'document'
    await loadGraph()
    if (value.status === 'running') watchRun(id)
  } catch (cause) {
    error.value = message(cause)
    if (run.value?.status === 'running') watchRun(run.value.id)
  } finally { if (sequence === loadSequence) loading.value = false }
}
async function loadGraph() {
  const sequence = ++graphSequence
  graphError.value = ''
  if (currentView.value === 'input') {
    graphLoading.value = false
    graph.value = inputGraph(source.value?.document)
    return
  }
  if (!run.value || !selectedDocument.value) { graph.value = { nodes: [], edges: [], warnings: [] }; return }
  const key = `${run.value.id}:${currentView.value}:${run.value.decision.status}`
  if (graphCache.has(key)) { graph.value = graphCache.get(key)!; graphLoading.value = false; return }
  graphLoading.value = true
  try {
    const value = await api<GraphResponse>(`/runs/${encodeURIComponent(run.value.id)}/graph?view=${currentView.value}`)
    if (sequence !== graphSequence) return
    graphCache.set(key, value)
    graph.value = value
  } catch (cause) {
    if (sequence !== graphSequence) return
    graphError.value = message(cause)
    graph.value = { nodes: [], edges: [], warnings: [] }
  } finally { if (sequence === graphSequence) graphLoading.value = false }
}
function inputGraph(doc?: StoryDocument): GraphResponse {
  if (!doc) return { nodes: [], edges: [], warnings: [] }
  const ref = documentRef(doc)
  const nodes = doc.entities.map(entity => ({ data: {
    id: `${ref}:${entity.id}`, label: entity.name, type: entity.type, description: entity.description,
    document_ref: ref, status: doc.kind === 'input' ? 'input' : doc.decision === 'pending' ? 'proposal' : doc.decision,
    basis: entity.basis, evidence: entity.evidence.map(item => ({ ...item, document_ref: item.document_ref === 'self' ? ref : item.document_ref })), issue_ids: [],
  } }))
  const ids = new Set(nodes.map(node => node.data.id))
  const edges = doc.relations.map(relation => ({ data: {
    id: `${ref}:${relation.id}`, label: relation.label, type: relation.type, condition: relation.condition,
    source: ids.has(relation.source) ? relation.source : `${ref}:${relation.source}`,
    target: ids.has(relation.target) ? relation.target : `${ref}:${relation.target}`,
    document_ref: ref, status: doc.kind === 'input' ? 'input' : doc.decision === 'pending' ? 'proposal' : doc.decision,
    basis: relation.basis, evidence: relation.evidence.map(item => ({ ...item, document_ref: item.document_ref === 'self' ? ref : item.document_ref })), issue_ids: [],
  } })).filter(edge => ids.has(edge.data.source) && ids.has(edge.data.target))
  const warnings = [...(doc.warnings || [])]
  if (doc.relations.length !== edges.length) warnings.push('끝점을 확인할 수 없는 관계는 표시에서 제외했습니다.')
  if (doc.graph_status === 'partial' && !warnings.length) warnings.push('일부 항목·관계의 구조를 확인하지 못했습니다. 본문 근거를 함께 확인해주세요.')
  if (doc.graph_status === 'unavailable') warnings.push('구조화된 그래프를 사용할 수 없습니다. 본문에서 설정을 확인해주세요.')
  return { nodes, edges, warnings }
}
function message(cause: unknown) { return cause instanceof Error ? cause.message : '처리 중 문제가 생겼습니다. 서버 상태를 확인해주세요.' }
async function analyze() {
  if (!canAnalyze.value) return
  analyzing.value = true
  error.value = ''
  feedback.value = ''
  try {
    if (!source.value) {
      const saved = await api<Source>('/sources', { text: sourceText.value, title: sourceTitle.value.trim() || undefined })
      source.value = saved
      remember('source', saved.id)
    }
    const result = await api<Analysis>(`/sources/${encodeURIComponent(source.value.id)}/analyze`, { request_id: analyzeRequest, trace: trace.value })
    analysis.value = result
    source.value = { ...source.value, document: result.document, analysis: result }
    selectedFacts.value = []
    currentView.value = 'input'
    await loadGraph()
    feedback.value = '재료를 정리했습니다. 지킬 조건은 선택 사항입니다. 「초안 요청으로 이동」에서 첫 설정을 만들어보세요.'
  } catch (cause) { error.value = message(cause) }
  finally { analyzing.value = false }
}
async function generate() {
  if (!canGenerate.value) return
  submitting.value = true
  error.value = ''
  feedback.value = ''
  try {
    const value = await api<Run>('/runs', {
      request_id: generationRequest,
      ...(continuing.value ? { accepted_run_id: continuing.value.id } : { analysis_id: analysis.value!.id }),
      constraints: constraints.value,
      request: request.value.trim(), trace: trace.value,
    })
    progress.value = []
    updateRun(value, true)
    request.value = value.request
    directConstraints.value = value.context.constraints.join('\n')
    selectedFacts.value = []
    continuing.value = null
    mobilePanel.value = 'document'
    if (value.status === 'running') watchRun(value.id)
    if (!source.value) {
      try { applySource(await api<Source>(`/sources/${encodeURIComponent(value.source_id)}`)) }
      catch { error.value = '작업은 시작되었습니다. 원문 표시를 복원하지 못했으니 완료 후 이력에서 다시 불러와주세요.' }
    }
    await loadGraph()
  } catch (cause) { error.value = message(cause) }
  finally { submitting.value = false }
}
function newWork() {
  if (busy.value) return
  ++loadSequence
  stopWatching()
  run.value = null
  continuing.value = null
  sourceText.value = ''
  sourceTitle.value = ''
  source.value = null
  analysis.value = null
  directConstraints.value = ''
  request.value = ''
  selectedFacts.value = []
  trace.value = false
  currentView.value = 'input'
  graph.value = { nodes: [], edges: [], warnings: [] }
  mode.value = 'body'
  mobilePanel.value = 'materials'
  error.value = ''
  feedback.value = ''
  progress.value = []
  resetSelection()
  analyzeRequest = requestId()
  generationRequest = requestId()
  remember('run', null)
  remember('source', null)
}
function continueStory() {
  if (!run.value || run.value.decision.status !== 'accepted' || busy.value) return
  const previous = run.value
  newWork()
  continuing.value = previous
  source.value = null
  request.value = ''
  feedback.value = '채택한 최종 설정과 기존 기준을 이어받았습니다. 다음에 펼칠 사건을 요청해주세요.'
  mobilePanel.value = 'document'
}
function useExample() {
  if (locked.value) return
  sourceTitle.value = '잊음의 검, 거짓 없는 검객'
  sourceText.value = '무협 / 기억을 잃는 대가로 강해지는 무공 / 거짓말을 못 하는 주인공\n\n주인공 서린은 무공을 쓸 때마다 소중한 기억 하나를 잃는다. 그녀는 거짓말을 할 수 없지만 침묵할 수는 있다. 잃어버린 기억은 돌아오지 않는다.'
}
async function importFile(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file || locked.value) return
  error.value = ''
  try {
    if (!/\.(txt|md)$/i.test(file.name)) throw new Error('TXT 또는 MD 파일만 가져올 수 있습니다.')
    if (file.size > 100 * 1024) throw new Error('파일은 100KB 이하여야 합니다. 필요한 부분만 남겨주세요.')
    const text = new TextDecoder('utf-8', { fatal: true }).decode(await file.arrayBuffer()).replace(/^\uFEFF/, '')
    if (text.length > 20000) throw new Error('입력은 20,000자 이내여야 합니다. 파일을 줄인 뒤 다시 가져와주세요.')
    if (!text.trim()) throw new Error('비어 있는 파일입니다. 키워드나 본문을 넣어주세요.')
    sourceText.value = text
    sourceTitle.value = file.name.replace(/\.(txt|md)$/i, '').slice(0, 200)
    feedback.value = `${file.name} 파일을 가져왔습니다. 아직 분석을 실행하지 않았습니다.`
  } catch (cause) { error.value = cause instanceof TypeError ? 'UTF-8로 인코딩한 TXT·MD 파일을 사용해주세요.' : message(cause) }
}
function findEvidence(evidence: Evidence, owner?: string) {
  const ref = evidence.document_ref === 'self' ? owner || (selectedDocument.value ? documentRef(selectedDocument.value) : '') : evidence.document_ref
  const doc = allDocuments.value.find(doc => documentRef(doc) === ref)
  return { doc, passage: doc?.passages.find(p => p.id === evidence.passage_id), ref }
}
async function jumpEvidence(evidence: Evidence, owner?: string) {
  const { doc, passage } = findEvidence(evidence, owner)
  if (!doc || !passage) { error.value = '해당 버전의 근거 문단을 찾을 수 없습니다. 이력을 다시 불러와주세요.'; return }
  evidenceDocument.value = doc
  highlightedPassage.value = `${documentRef(doc)}:${passage.id}`
  mode.value = 'body'
  mobilePanel.value = 'document'
  await nextTick()
  document.getElementById(`passage-${passage.id}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' })
}
function chooseItem(data: GraphData) {
  selectedItem.value = data
  mobilePanel.value = 'review'
}
function chooseIssue(issue: Issue) {
  selectedIssue.value = selectedIssue.value?.id === issue.id ? null : issue
  selectedItem.value = null
}
async function decide(status: 'accepted' | 'deferred') {
  if (!run.value || !decisionAllowed.value || deciding.value) return
  ++refreshSequence
  deciding.value = true
  error.value = ''
  try {
    const value = await api<Run>(`/runs/${encodeURIComponent(run.value.id)}/decision`, { status, reason: decisionReason.value.trim() })
    updateRun(value)
    evidenceDocument.value = null
    highlightedPassage.value = ''
    graphCache.clear()
    await loadGraph()
    feedback.value = status === 'accepted' ? '최종 문서 전체를 채택했습니다. 「이 설정 이어쓰기」로 다음 사건을 만들 수 있습니다. 남은 쟁점도 함께 보존됩니다.' : '최종 문서 전체를 보류했습니다. 다음 작업의 기준에 자동 포함되지 않습니다.'
  } catch (cause) { error.value = message(cause) }
  finally { deciding.value = false }
}
async function exportDocument() {
  if (!run.value) return
  try {
    const response = await fetch(`/api/runs/${encodeURIComponent(run.value.id)}/export`)
    if (!response.ok) throw new Error('내보내기에 실패했습니다. 저장 상태를 다시 확인해주세요.')
    const blob = await response.blob()
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `LoreForge-${(finalDocument.value?.title || run.value.id).replace(/[\\/:*?"<>|]/g, '_')}.md`
    link.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
    feedback.value = '본문·근거·검토·결정이 담긴 Markdown 다운로드를 시작했습니다.'
  } catch (cause) { error.value = message(cause) }
}
function dateLabel(value: string) {
  return new Intl.DateTimeFormat('ko-KR', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date(value))
}
async function initialize() {
  loading.value = true
  const results = await Promise.allSettled([api<Health>('/health'), api<{ runs: Run[] }>('/runs')])
  if (results[0].status === 'fulfilled') health.value = results[0].value
  else error.value = `서버에 연결하지 못했습니다. ${message(results[0].reason)}`
  if (results[1].status === 'fulfilled') history.value = results[1].value.runs
  else if (!error.value) error.value = '작업 이력을 불러오지 못했습니다. 서버 연결을 확인해주세요.'
  const active = recall('run')
  if (active) await openRun(active)
  else {
    const savedSource = recall('source')
    if (savedSource) {
      try { applySource(await api<Source>(`/sources/${encodeURIComponent(savedSource)}`)); await loadGraph() }
      catch (cause) { error.value = `저장된 재료를 복원하지 못했습니다. ${message(cause)}` }
    }
  }
  loading.value = false
}
onMounted(initialize)
onBeforeUnmount(stopWatching)
</script>

<template>
  <div class="workbench">
    <header class="app-header">
      <a class="brand" href="#" aria-label="설정공방 작업실" @click.prevent="mobilePanel = 'document'">
        <span class="brand-mark"><svg viewBox="0 0 28 28" fill="none" aria-hidden="true"><path d="M22 4C14 3 7 9 7 19l-3 5 5-4C19 19 25 11 22 4Z" stroke="currentColor" stroke-width="1.3"/><path d="m7 20 12-12M12 15l-1-5m5 1h5" stroke="currentColor" stroke-width="1.2"/></svg></span>
        <span>설정공방<small>LOREFORGE</small></span>
      </a>
      <div class="header-location"><span>/</span> 나의 작업실 <span class="local-label">LOCAL WORKSPACE</span></div>
      <div class="header-status"><span class="connection-dot" :class="{ online: health }" />{{ health ? '로컬 저장소 연결됨' : '서버 연결 확인 중' }}</div>
      <button class="new-work" :disabled="busy" @click="newWork"><span>＋</span> 새 작업</button>
    </header>

    <div v-if="health?.provider === 'mock'" class="provider-banner mock" role="status"><span class="banner-dot" /><strong>모의 시연 모드</strong><span>고정된 가상 응답으로 동작합니다. 실제 AI 생성·품질 검증 결과가 아닙니다.</span><span class="banner-tag">MOCK</span></div>
    <div v-else-if="health?.provider === 'openai'" class="provider-banner"><span class="banner-dot" /><strong>OpenAI API</strong><span>{{ health.key_configured ? '분석·생성 실행 시 재료와 조건이 OpenAI로 전송됩니다.' : '서버에 API 키가 없습니다. 서버 환경 설정 후 다시 연결해주세요.' }}</span><span class="banner-tag">{{ health.key_configured ? '연결 준비됨' : '설정 필요' }}</span></div>
    <div v-else class="provider-banner"><strong>서버 연결 대기</strong><span>저장소와 모델 설정을 확인하고 있습니다.</span><button v-if="!loading" class="text-button" @click="initialize">다시 연결</button></div>

    <div v-if="error" class="global-message error-message" role="alert"><span>!</span><p>{{ error }}</p><button aria-label="오류 메시지 닫기" @click="error = ''">×</button></div>
    <div v-if="feedback" class="global-message feedback-message" role="status"><span>✓</span><p>{{ feedback }}</p><button aria-label="알림 닫기" @click="feedback = ''">×</button></div>

    <nav class="mobile-navigation" aria-label="작업실 패널"><button :class="{ active: mobilePanel === 'materials' }" @click="mobilePanel = 'materials'">01 재료</button><button :class="{ active: mobilePanel === 'document' }" @click="mobilePanel = 'document'">02 작업 문서</button><button :class="{ active: mobilePanel === 'review' }" @click="mobilePanel = 'review'">03 검토<span v-if="unresolved" class="tiny-count">{{ unresolved }}</span></button></nav>

    <main class="workspace-grid">
      <aside class="materials-panel panel" :class="{ 'mobile-active': mobilePanel === 'materials' }" aria-label="입력 재료와 실행 이력">
        <div class="panel-heading"><div><span class="eyebrow">YOUR MATERIALS</span><h1>이야기의 씨앗</h1></div><span class="section-number">01</span></div>
        <div v-if="continuing" class="continuation-card"><span class="badge accepted">채택한 설정 이어쓰기</span><strong>{{ continuing.revision?.title || continuing.draft?.title }}</strong><p>기존 기준과 채택한 최종 문서를 이어받습니다.</p><small>{{ continuationDocuments.length }}개 문서 · {{ contextLength.toLocaleString() }}자 기준</small></div>
        <template v-else>
          <p class="panel-intro">키워드 몇 개도, 오래 모은 설정도.<br />흩어진 생각을 이곳에 놓아주세요.</p>
          <label for="source-title" class="field-label">작업 제목 <span>선택</span></label>
          <input id="source-title" v-model="sourceTitle" :disabled="locked" placeholder="아직 이름 없는 세계" autocomplete="off" />
          <div class="label-row"><label for="source-text" class="field-label">키워드 · 메모 · 설정 본문</label><button class="text-button" :disabled="locked" @click="useExample">예시 넣기 ↗</button></div>
          <textarea id="source-text" v-model="sourceText" class="source-input" :disabled="locked" placeholder="무협 / 기억을 잃는 대가로 강해지는 무공 / 거짓말을 못 하는 주인공&#10;&#10;키워드나 설정을 자유롭게 적어주세요." />
          <div class="input-meta"><button class="text-button import-button" :disabled="locked" @click="fileInput?.click()"><span>↥</span> TXT · MD 가져오기</button><span :class="{ 'limit-exceeded': sourceText.length > 20000 }">{{ sourceText.length.toLocaleString() }} / 20,000</span></div>
          <input ref="fileInput" class="sr-only" type="file" accept=".txt,.md,text/plain,text/markdown" aria-label="UTF-8 TXT 또는 MD 파일 가져오기" :disabled="locked" @change="importFile" />
          <p class="field-hint">UTF-8 · 최대 100KB. 가져온 원문은 보존됩니다.</p>
        </template>

        <section class="constraints-section">
          <div class="label-row"><h2>지켜야 할 조건</h2><span class="optional-label">선택</span></div>
          <p class="field-hint">꼭 지킬 내용이 있다면 한 줄에 하나씩 적어주세요. 비워두어도 시작할 수 있어요.</p>
          <textarea id="constraints" v-model="directConstraints" rows="3" :disabled="Boolean(run) || busy" placeholder="예: 잃어버린 기억은 돌아오지 않는다" aria-label="반드시 지킬 조건" />
          <div v-if="analysis?.facts.length && !run" class="fact-candidates">
            <div class="candidate-heading">재료에서 찾은 후보 <span>{{ selectedFacts.length }}개 선택</span></div>
            <p class="field-hint">AI가 정리한 후보입니다. 지킬 내용만 골라주세요.</p>
            <div v-for="fact in analysis.facts" :key="fact.id" class="fact-candidate">
              <label><input v-model="selectedFacts" type="checkbox" :value="fact.id" :disabled="busy" /><span>{{ fact.text }}<small>{{ basisLabel(fact.basis) }} · {{ fact.kind === 'condition' ? '조건 후보' : '설정 후보' }}</small></span></label>
              <button v-for="(evidence, i) in fact.evidence" :key="i" class="evidence-inline" @click="jumpEvidence(evidence, documentRef(analysis.document))">{{ evidenceLabel(analysis.document, evidence.passage_id) }} 보기 ↗</button>
            </div>
          </div>
          <p v-if="contextLength > 20000" class="inline-error">기준 본문이 20,000자를 넘었습니다. 재료나 조건을 줄여주세요.</p>
          <p v-else-if="constraints.length" class="field-hint condition-count">{{ constraints.length }}개 조건 · 전체 기준 {{ contextLength.toLocaleString() }}자</p>
        </section>

        <div v-if="!run" class="prepare-actions">
          <button v-if="!analysis && !continuing" class="primary-button full-width" :disabled="!canAnalyze" @click="analyze"><span v-if="analyzing" class="spinner" />{{ analyzing ? '재료를 정리하고 있어요' : '재료 정리하기' }}<span v-if="!analyzing">→</span></button>
          <template v-else-if="analysis"><div class="prepared-label"><span>✓</span> 재료 정리 완료 <small>조건을 고르거나 바로 첫 설정을 만들 수 있어요.</small></div><button class="full-width" @click="focusPanel('document', 'story-request')">초안 요청으로 이동 →</button></template>
          <p v-if="!canAnalyze && !analysis && !continuing" class="field-hint">{{ disableReason }}</p>
        </div>

        <section class="history-section">
          <div class="label-row"><h2>작업 이력</h2><span class="optional-label">{{ history.length }}</span></div>
          <p v-if="!history.length" class="history-empty">이야기를 시작하면 이곳에 기록됩니다.</p>
          <button v-for="item in history" :key="item.id" class="history-item" :class="{ current: run?.id === item.id }" :disabled="analyzing || submitting || loading" @click="openRun(item.id)">
            <span class="history-document">▤</span><span class="history-copy"><strong>{{ item.revision?.title || item.draft?.title || item.request || '새로운 이야기' }}</strong><small>{{ dateLabel(item.created_at) }}<span>{{ statusLabel[item.status] || item.status }}</span></small></span><span v-if="item.decision.status === 'accepted'" class="history-check" aria-label="채택됨">✓</span><span v-else-if="item.decision.status === 'deferred'" class="history-check muted" aria-label="보류됨">Ⅱ</span>
          </button>
        </section>
        <div class="sidebar-foot"><span>◇</span> 생각의 흔적을 남기는 작업실</div>
      </aside>

      <section class="document-panel panel" :class="{ 'mobile-active': mobilePanel === 'document' }" aria-label="본문과 관계 그래프">
        <div class="document-toolbar"><div class="version-tabs" aria-label="문서 버전"><button v-for="(label, value) in viewLabels" :key="value" :class="{ active: currentView === value }" :disabled="value === 'input' ? !source : !run?.[value]" @click="currentView = value">{{ label }}<span v-if="value === 'revision' && run?.revision" class="version-dot" /></button></div><div class="view-switch" aria-label="보기 방식"><button :class="{ active: mode === 'body' }" aria-label="본문 보기" @click="mode = 'body'">☷ <span>본문</span></button><button :class="{ active: mode === 'graph' }" aria-label="관계 그래프 보기" @click="mode = 'graph'">⌘ <span>관계</span></button></div></div>

        <div v-if="analyzing || run" class="run-progress" :class="{ working: analyzing || running }" role="status" aria-live="polite">
          <div class="progress-head"><span v-if="analyzing || running" class="spinner" /><span v-else class="status-symbol">{{ run?.status === 'completed' ? '✓' : '◷' }}</span><strong>{{ stageText }}</strong><span v-if="run" class="status-caption">{{ statusLabel[run.status] || run.status }}</span></div>
          <div v-if="analyzing || running" class="progress-track"><span /></div>
          <div v-if="progress.length" class="latest-progress">{{ progress[progress.length - 1]?.message }}</div>
          <details v-if="progress.length > 1" class="progress-details"><summary>실행 단계 기록</summary><p v-for="item in progress" :key="item.id"><span>{{ stageNames[item.stage] || item.stage }}</span>{{ item.message }}</p></details>
          <div v-if="usage" class="usage-line"><span>호출 {{ usage.attempts || 0 }}회 / 최대 6회</span><span>재시도 {{ usage.retries || 0 }}회</span><span>{{ (usage.elapsed_seconds || 0).toFixed(1) }}초</span><span>입력 {{ (usage.input_tokens || 0).toLocaleString() }} · 출력 {{ (usage.output_tokens || 0).toLocaleString() }} 토큰</span></div>
          <p v-if="run?.error" class="inline-error">{{ run.error }}</p>
          <p v-if="run?.status === 'interrupted'" class="field-hint">서버가 재시작되어 중단된 작업입니다. 유료 호출을 자동 재개하지 않습니다. 새 작업으로 다시 시작해주세요.</p>
          <p v-if="runWarnings.length" class="field-hint">실행 전체 경고입니다. 이전 초안의 경고도 보존합니다.</p>
          <p v-for="warning in runWarnings" :key="warning.message" class="notice warning">{{ warning.message }}<span v-if="warning.count > 1"> · 같은 내용의 경고 {{ warning.count }}건</span></p>
        </div>

        <div class="document-content">
          <template v-if="mode === 'graph'">
            <div v-if="graphLoading" class="center-loading"><span class="spinner" />저장된 관계를 불러오는 중</div>
            <div v-else-if="graphError" class="empty-state compact"><span>◇</span><h2>그래프를 불러오지 못했어요</h2><p>{{ graphError }}</p><button @click="loadGraph">다시 불러오기</button></div>
            <div v-else-if="!selectedDocument" class="empty-state compact"><span>◇</span><h2>{{ running ? '관계도를 준비하고 있어요' : '재료를 정리하면 관계를 볼 수 있어요' }}</h2><p>{{ running ? '초안이 저장되면 인물과 관계를 이곳에서 살펴볼 수 있습니다.' : continuing ? '이야기 이어가기를 실행하면 새 초안의 관계를 살펴볼 수 있습니다.' : '키워드나 메모를 입력하고 「재료 정리하기」를 눌러주세요.' }}</p><button v-if="!run && !continuing" @click="focusPanel('materials', 'source-text')">재료 입력으로 이동 →</button></div>
            <RelationshipGraph v-else :graph="graph" :issue-id="selectedIssue?.id" :issue-evidence="selectedIssue?.evidence" @select="chooseItem" />
          </template>
          <template v-else-if="bodyDocument">
            <div v-if="evidenceDocument" class="evidence-jump-banner"><span>근거 문서 · {{ bodyDocument.kind === 'input' ? '입력 자료' : bodyDocument.decision === 'accepted' ? '채택한 설정' : bodyDocument.kind === 'revision' ? '수정본' : '초안' }}</span><button class="text-button" @click="evidenceDocument = null; highlightedPassage = ''">선택 버전으로 돌아가기 ↩</button></div>
            <article class="story-document"><div class="document-kicker"><span class="badge" :class="bodyDocument.decision">{{ bodyDocument.kind === 'input' ? '입력 원문' : statusLabel[bodyDocument.decision] || 'AI 제안' }}</span><span>{{ viewLabels[bodyDocument.kind as DocumentView] || '기준 문서' }} · v{{ String(bodyDocument.version).slice(0, 8) }}</span></div><h2>{{ bodyDocument.title || sourceTitle || '아직 이름 없는 세계' }}</h2><div class="title-rule"><span /></div><p class="field-hint passage-guide">문단 번호는 근거 위치를 찾기 위한 표시입니다. 오른쪽 근거를 누르면 해당 문단으로 이동합니다.</p><section v-for="passage in bodyDocument.passages" :id="`passage-${passage.id}`" :key="passage.id" class="passage" :class="{ highlighted: highlightedPassage === `${documentRef(bodyDocument)}:${passage.id}` }"><span class="passage-id">{{ passageLabel(bodyDocument, passage.id) }}</span><p>{{ passage.text }}</p></section><footer class="document-footer"><span>◇</span> {{ bodyDocument.kind === 'input' ? '입력 원문은 바꾸지 않고 보존합니다.' : 'AI가 제안한 설정입니다. 채택 여부와 남은 쟁점을 확인해주세요.' }}</footer></article>
          </template>
          <div v-else-if="continuing" class="empty-state continuation-empty"><span class="empty-emblem">↗</span><span class="eyebrow">THE NEXT CHAPTER</span><h2>다음 이야기는<br />어디로 향할까요?</h2><p>채택한 설정을 기준으로 다음 사건을 제안합니다.<br />아래에 원하는 전개를 적거나, 바로 이어가세요.</p><div class="context-preview" v-for="doc in continuationDocuments" :key="documentRef(doc)"><span>{{ doc.kind === 'input' ? '기존 입력' : '채택 설정' }}</span><strong>{{ doc.title || '입력 자료' }}</strong><small>{{ doc.passages.length }}개 문단</small></div></div>
          <div v-else class="empty-state"><div class="manuscript-art" aria-hidden="true"><div class="art-sheet"><span>WORLD NOTES</span><i /><i /><i /><i /><b>◇</b></div><div class="orbit orbit-one" /><div class="orbit orbit-two" /><span class="art-spark spark-one">✧</span><span class="art-spark spark-two">·</span></div><span class="eyebrow">EVERY WORLD BEGINS WITH A SEED</span><h2>작은 생각에서,<br />하나의 세계로.</h2><p>「이야기의 씨앗」에 키워드나 메모를 적고<br />「재료 정리하기」를 눌러 시작하세요.</p><div class="welcome-steps"><span><b>01</b> 재료 정리</span><i>→</i><span><b>02</b> 초안과 검토</span><i>→</i><span><b>03</b> 검토 후 채택</span></div></div>
        </div>

        <div class="request-composer" :class="{ inactive: Boolean(run) }"><div class="label-row"><label for="story-request" class="field-label">{{ continuing ? '이 설정에서 이어갈 이야기' : run ? '이번 작업의 요청' : '어떤 이야기로 펼쳐볼까요?' }} <span v-if="!run">선택</span></label><span class="request-count" :class="{ 'limit-exceeded': request.length > 2000 }">{{ request.length.toLocaleString() }} / 2,000</span></div><textarea id="story-request" v-model="request" rows="2" :disabled="Boolean(run) || busy" :placeholder="continuing ? '예: 주인공이 잃어버린 기억의 주인을 만나는 사건을 써줘' : '예: 주인공의 첫 번째 선택과 그 대가를 구체화해줘'" /><div class="composer-footer"><p>{{ run ? '기준과 실제 요청은 실행 기록에 보존됩니다.' : !analysis && !continuing ? '먼저 재료를 입력하고 「재료 정리하기」를 눌러주세요.' : continuing ? '비워두면 채택한 설정을 지키며 다음 사건 하나를 만듭니다.' : '요청은 비워두어도 됩니다. 첫 설정을 만든 뒤 AI가 검토하고 최대 한 번 수정합니다.' }}</p><button v-if="!run" class="primary-button generate-button" :disabled="!canGenerate" @click="generate"><span v-if="submitting" class="spinner" />{{ submitting ? '작업 시작 중' : continuing ? '이야기 이어가기' : '첫 설정 만들기' }}<span v-if="!submitting">↗</span></button></div><button v-if="decisionAllowed && !running" class="text-button review-next-action" @click="focusPanel('review', 'review-heading')">근거와 검토를 확인하고 채택 여부 결정 →</button><div v-if="!run" class="trace-option"><label><input v-model="trace" type="checkbox" :disabled="busy" /><span>공개용 가상 자료임을 확인했으며 LangSmith 외부 추적에 동의합니다.</span></label><small>선택 시 단계·모델·토큰 등 실행 메타데이터를 전송합니다. 본문은 추적에 기록하지 않습니다.</small></div><p v-if="!run && !canGenerate && (analysis || continuing || request.length > 2000)" class="field-hint">{{ disableReason || '재료 정리를 마친 뒤 생성할 수 있습니다.' }}</p></div>
      </section>

      <aside class="review-panel panel" :class="{ 'mobile-active': mobilePanel === 'review' }" aria-label="근거 상세와 검토·채택">
        <div class="panel-heading"><div><span class="eyebrow">EVIDENCE & REVIEW</span><h2 id="review-heading" tabindex="-1">근거와 나의 판단</h2></div><span class="section-number">03</span></div>
        <section v-if="selectedItem" class="item-detail"><div class="detail-topline"><span class="badge">{{ selectedItem.source ? '관계' : kindLabel[selectedItem.type] || selectedItem.type }}</span><button class="text-button" aria-label="선택 상세 닫기" @click="selectedItem = null">×</button></div><h3>{{ selectedItem.label }}</h3><div class="detail-badges"><span>{{ statusLabel[selectedItem.status] || selectedItem.status }}</span><span>{{ basisLabel(selectedItem.basis) }}</span></div><p v-if="selectedItem.description">{{ selectedItem.description }}</p><p v-if="selectedItem.condition" class="condition-detail">적용 조건 · {{ selectedItem.condition }}</p><div v-if="selectedItem.source" class="relation-direction">{{ graph.nodes.find(n => n.data.id === selectedItem?.source)?.data.label || selectedItem.source }} <span>→ {{ selectedItem.label }} →</span> {{ graph.nodes.find(n => n.data.id === selectedItem?.target)?.data.label || selectedItem.target }}</div><span class="detail-label">본문 근거</span><button v-for="(evidence, i) in selectedItem.evidence" :key="i" class="evidence-card" @click="jumpEvidence(evidence, selectedItem.document_ref)"><span>{{ evidenceLabel(findEvidence(evidence, selectedItem.document_ref).doc, evidence.passage_id) }} <b>↗</b></span><small>{{ findEvidence(evidence, selectedItem.document_ref).doc?.title }}</small><p>{{ findEvidence(evidence, selectedItem.document_ref).passage?.text || '근거 문단을 확인할 수 없습니다.' }}</p></button><p v-if="!selectedItem.evidence.length" class="field-hint">연결된 근거 문단이 없습니다.</p><p v-if="selectedItem.issue_ids?.length" class="field-hint">이 항목과 근거가 겹치는 쟁점이 {{ selectedItem.issue_ids.length }}개 있습니다. 아래 검토 목록을 확인해주세요.</p><p class="field-hint">선택은 탐색 동작입니다. 생성이나 개별 채택을 실행하지 않습니다.</p></section>

        <section class="review-section"><div class="review-title-row"><h3>{{ currentView === 'revision' ? '수정본 검토' : currentView === 'draft' ? '초안 검토' : '검토 노트' }}</h3><span v-if="currentIssues.length" class="issue-count">{{ unresolved }}개 미해결</span></div><p class="field-hint">AI의 검토는 판단을 돕는 단서입니다.<br />이야기의 의도와 최종 선택은 창작자에게 있습니다.</p>
          <div v-if="!run || currentView === 'input'" class="review-placeholder"><span>⌕</span><p>초안이 만들어지면<br />충돌 후보와 질문이 이곳에 모입니다.</p><div class="review-placeholder-line" /><small>기준 · 초안 · 근거를 함께 확인해요</small></div>
          <div v-else-if="running && !run.reviews.some(r => r.document_ref === (selectedDocument ? documentRef(selectedDocument) : ''))" class="review-placeholder"><span class="spinner" /><p>본문 근거를 검토하고 있습니다.</p></div>
          <div v-else-if="!currentIssues.length" class="review-placeholder"><span>✓</span><p>{{ run.reviews.some(r => r.document_ref === (selectedDocument ? documentRef(selectedDocument) : '')) ? '이 버전의 검토에서 기록된 쟁점이 없습니다.' : '이 버전의 검토 결과가 없습니다.' }}</p><small>모든 설정의 일관성을 보장하지 않습니다.</small></div>
          <div v-for="issue in currentIssues" :key="issue.id" class="issue-card" :class="{ selected: selectedIssue?.id === issue.id, resolved: issue.status === 'resolved' }"><button class="issue-toggle" :aria-expanded="selectedIssue?.id === issue.id" @click="chooseIssue(issue)"><span class="issue-kind">{{ issueLabel[issue.kind] || issue.kind }}</span><span class="issue-state">{{ issue.status === 'resolved' ? '해결됨' : '확인 필요' }}</span><strong>{{ issue.reason }}</strong><span class="issue-more">{{ selectedIssue?.id === issue.id ? '접기 −' : '근거와 제안 보기 +' }}</span></button><div v-if="selectedIssue?.id === issue.id" class="issue-expanded"><p class="issue-suggestion"><span>수정 제안</span>{{ issue.suggestion }}</p><button v-for="(evidence, i) in issue.evidence" :key="i" class="evidence-card" @click="jumpEvidence(evidence)"><span>{{ evidenceLabel(findEvidence(evidence).doc, evidence.passage_id) }} <b>↗</b></span><small>{{ findEvidence(evidence).doc?.title }}</small><p>{{ findEvidence(evidence).passage?.text || '근거 문단을 확인할 수 없습니다.' }}</p></button><button class="text-button graph-issue-link" @click="mode = 'graph'; mobilePanel = 'document'">관계에서 관련 근거 살펴보기 ↗</button></div></div>
        </section>

        <section v-if="pendingIssues.length" class="pending-issues" aria-label="이전 검토의 미해결 쟁점">
          <h3>이전 검토에서 남은 확인 사항 · {{ pendingIssues.length }}</h3>
          <p class="field-hint">아래는 원래 문서에서 남은 쟁점입니다. 현재 버전의 검토 결과와 구분해 보존하며, 채택하거나 이어써도 자동으로 해결되지 않습니다.</p>
          <details v-for="entry in pendingIssues" :key="`${entry.origin_run_id}:${entry.document_ref}:${entry.issue.id}`" class="pending-issue">
            <summary><span class="issue-kind">{{ issueLabel[entry.issue.kind] || entry.issue.kind }}</span> {{ entry.issue.reason }}</summary>
            <p>{{ entry.issue.suggestion }}</p>
            <small>원래 검토 문서: {{ entry.document_ref }}</small>
            <blockquote v-for="evidence in entry.evidence_snapshots" :key="`${evidence.document_ref}:${evidence.passage_id}`"><small>{{ evidenceLabel(findEvidence(evidence).doc, evidence.passage_id) }}</small><p>{{ evidence.text }}</p></blockquote>
          </details>
        </section>

        <section v-if="run && !running && finalDocument" class="decision-section"><div class="label-row"><h3>이 설정을 어떻게 남길까요?</h3><span v-if="run.decision.status !== 'pending'" class="badge" :class="run.decision.status">{{ run.decision.status === 'accepted' ? '채택됨' : '보류됨' }}</span></div><p class="field-hint">{{ run.revision ? '수정본' : '초안' }} 전체에 적용합니다. 채택하면 이어쓰기의 기준으로 사용할 수 있습니다. 보류하면 기준에서 제외합니다. 남은 쟁점도 보존됩니다.</p><p v-if="!decisionAllowed" class="notice warning">생성·검토가 정상적으로 끝난 결과에만 채택·보류할 수 있습니다. 현재 결과는 읽기와 내보내기만 가능합니다.</p><p v-if="!finalView" class="notice warning">현재는 {{ viewLabels[currentView] }}을 보고 있습니다. 결정은 최종 {{ run.revision ? '수정본' : '초안' }}에 적용됩니다. <button class="text-button" @click="showFinalDocument">최종 {{ run.revision ? '수정본' : '초안' }} 보기 ↗</button></p><label for="decision-reason" class="field-label">판단 이유 <span>선택</span></label><textarea id="decision-reason" v-model="decisionReason" rows="2" :disabled="deciding" placeholder="남길 설정과 더 생각할 부분을 적어주세요" /><div class="decision-buttons"><button class="primary-button" :disabled="deciding || !decisionAllowed" @click="decide('accepted')">{{ deciding ? '저장 중' : '✓ 이 설정 채택' }}</button><button :disabled="deciding || !decisionAllowed" @click="decide('deferred')">보류</button></div><button v-if="run.decision.status === 'accepted'" class="continue-button" :disabled="busy" @click="continueStory">이 설정 이어쓰기 <span>→</span></button><button class="export-button" @click="exportDocument"><span>↓</span> 근거와 함께 Markdown 내보내기</button></section>
        <div class="review-foot"><span class="mini-flower">✧</span><p>설정은 연결하고,<br />선택은 당신에게 남깁니다.</p><small>원문 보존 · 자동 수정 최대 1회</small></div>
      </aside>
    </main>
    <footer class="app-footer"><span>설정공방 · LoreForge</span><span>본문이 기준이고, 그래프는 탐색을 돕습니다.</span><span>LOCAL FIRST / AI ASSISTED</span></footer>
  </div>
</template>
