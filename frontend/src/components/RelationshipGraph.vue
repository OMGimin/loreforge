<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import cytoscape from 'cytoscape'
import type { Core, ElementDefinition } from 'cytoscape'
import type { Evidence, GraphData, GraphItem, GraphResponse } from '../types'
import { basisLabel, kindLabel, statusLabel } from '../types'

const props = defineProps<{ graph: GraphResponse; issueId?: string; issueEvidence?: Evidence[] }>()
const emit = defineEmits<{ select: [data: GraphData] }>()
const host = ref<HTMLDivElement>()
const type = ref('all')
const status = ref('all')
const query = ref('')
const relations = ref(true)
const onlyNeighbors = ref(false)
const selectedId = ref('')
let cy: Core | undefined
let observer: ResizeObserver | undefined

const filteredNodes = computed(() => props.graph.nodes.filter(({ data }) => {
  const stateMatches = status.value === 'all' || (status.value === 'proposal'
    ? ['proposal', 'pending', 'draft', 'revision'].includes(data.status) : data.status === status.value)
  return (type.value === 'all' || data.type === type.value) && stateMatches
    && (!query.value || `${data.label} ${data.description || ''}`.toLocaleLowerCase().includes(query.value.toLocaleLowerCase()))
}))
const neighborIds = computed(() => {
  const ids = new Set([selectedId.value])
  const selectedEdge = props.graph.edges.find(x => x.data.id === selectedId.value)
  if (selectedEdge) {
    ids.add(selectedEdge.data.source || '')
    ids.add(selectedEdge.data.target || '')
  }
  props.graph.edges.forEach(({ data }) => {
    if (data.source === selectedId.value) ids.add(data.target || '')
    if (data.target === selectedId.value) ids.add(data.source || '')
  })
  return ids
})
const scopedNodes = computed(() => onlyNeighbors.value && selectedId.value
  ? filteredNodes.value.filter(x => neighborIds.value.has(x.data.id)) : filteredNodes.value)
const visibleNodes = computed(() => scopedNodes.value.slice(0, 30))
const scopedEdges = computed(() => {
  const ids = new Set(visibleNodes.value.map(x => x.data.id))
  return relations.value ? props.graph.edges.filter(x => ids.has(x.data.source || '') && ids.has(x.data.target || '')) : []
})
const visibleEdges = computed(() => scopedEdges.value.slice(0, 60))
const limited = computed(() => scopedNodes.value.length > 30 || scopedEdges.value.length > 60)
const list = computed(() => [...visibleNodes.value, ...visibleEdges.value])

function issueMatches(data: GraphData) {
  if (!props.issueId) return false
  return data.issue_ids?.includes(props.issueId) || data.evidence?.some(evidence =>
    props.issueEvidence?.some(other => other.document_ref === (evidence.document_ref === 'self' ? data.document_ref : evidence.document_ref)
      && other.passage_id === evidence.passage_id))
}
function highlight() {
  if (!cy) return
  cy.elements().removeClass('selected-node neighbor dim issue-match')
  if (props.issueId) {
    cy.elements().forEach(element => {
      if (issueMatches(element.data() as GraphData)) element.addClass('issue-match')
    })
  }
  const chosen = cy.getElementById(selectedId.value)
  if (chosen.length) {
    const related = chosen.closedNeighborhood()
    cy.elements().difference(related).addClass('dim')
    related.addClass('neighbor')
    chosen.addClass('selected-node')
  }
}
function choose(data: GraphData) {
  selectedId.value = data.id
  emit('select', data)
  highlight()
}
function render() {
  if (!host.value || !cy) return
  const elements: GraphItem[] = [...visibleNodes.value, ...visibleEdges.value]
  cy.elements().remove()
  cy.add(elements as ElementDefinition[])
  cy.layout({ name: 'cose', animate: false, randomize: false, padding: 45, nodeRepulsion: () => 8500, idealEdgeLength: () => 105 }).run()
  highlight()
}
function zoom(factor: number) {
  if (!cy) return
  cy.zoom({ level: Math.min(2.5, Math.max(0.25, cy.zoom() * factor)), renderedPosition: { x: cy.width() / 2, y: cy.height() / 2 } })
}

onMounted(() => {
  cy = cytoscape({
    container: host.value,
    minZoom: 0.25,
    maxZoom: 2.5,
    wheelSensitivity: 0.2,
    style: [
      { selector: 'node', style: { label: 'data(label)', 'background-color': '#819f99', color: '#d9e3dc', 'font-family': 'system-ui, sans-serif', 'font-size': 11, 'text-valign': 'bottom', 'text-margin-y': 10, width: 27, height: 27, 'border-width': 1.5, 'border-color': '#b1cbc0', 'text-wrap': 'wrap', 'text-max-width': '105px', 'overlay-opacity': 0 } },
      { selector: 'node[type="person"]', style: { 'background-color': '#c5b080', 'border-color': '#ead4a4' } },
      { selector: 'node[type="place"]', style: { shape: 'round-rectangle', 'background-color': '#7f999c' } },
      { selector: 'node[type="group"]', style: { shape: 'hexagon', 'background-color': '#a593b5' } },
      { selector: 'node[type="event"]', style: { shape: 'diamond', 'background-color': '#b78773', width: 34, height: 34 } },
      { selector: 'node[type="rule"]', style: { shape: 'round-triangle', 'background-color': '#819c79', width: 32, height: 32 } },
      { selector: 'edge', style: { label: 'data(label)', width: 1.2, 'curve-style': 'bezier', 'line-color': '#596b62', 'target-arrow-color': '#7a8f81', 'target-arrow-shape': 'triangle', 'arrow-scale': 0.8, color: '#b2bdb4', 'font-size': 9, 'text-rotation': 'autorotate', 'text-background-color': '#171e1a', 'text-background-opacity': 0.9, 'text-background-padding': '3px', 'text-margin-y': -6, 'overlay-opacity': 0 } },
      { selector: '[basis="inferred"]', style: { 'line-style': 'dashed', 'border-style': 'dashed' } },
      { selector: 'node[status="accepted"]', style: { 'border-width': 3, 'border-style': 'double' } },
      { selector: '.dim', style: { opacity: 0.22 } },
      { selector: '.neighbor', style: { opacity: 1 } },
      { selector: '.issue-match', style: { 'border-color': '#e1a76d', 'border-width': 4, 'line-color': '#e1a76d', 'target-arrow-color': '#e1a76d', 'underlay-color': '#e1a76d', 'underlay-opacity': 0.14, 'underlay-padding': 8, opacity: 1 } },
      { selector: '.selected-node', style: { 'border-color': '#e8d8b4', 'border-width': 4, 'line-color': '#e8d8b4', 'target-arrow-color': '#e8d8b4', 'underlay-color': '#d4c49c', 'underlay-opacity': 0.16, 'underlay-padding': 10, opacity: 1 } },
    ],
  })
  cy.on('tap', 'node, edge', event => choose(event.target.data() as GraphData))
  cy.on('tap', event => { if (event.target === cy) { selectedId.value = ''; highlight() } })
  observer = new ResizeObserver(() => { cy?.resize() })
  if (host.value) observer.observe(host.value)
  render()
})
watch([visibleNodes, visibleEdges], () => nextTick(render))
watch(() => [props.issueId, props.issueEvidence], highlight, { deep: true })
watch(() => props.graph, () => { selectedId.value = ''; onlyNeighbors.value = false })
onBeforeUnmount(() => { observer?.disconnect(); cy?.destroy() })
</script>

<template>
  <div class="graph-view">
    <div class="graph-filters">
      <label class="sr-only" for="graph-search">그래프 항목 검색</label>
      <input id="graph-search" v-model="query" type="search" placeholder="항목 찾기" />
      <label class="sr-only" for="graph-type">항목 종류</label>
      <select id="graph-type" v-model="type"><option value="all">모든 종류</option><option v-for="(label, value) in kindLabel" :key="value" :value="value">{{ label }}</option></select>
      <label class="sr-only" for="graph-status">설정 상태</label>
      <select id="graph-status" v-model="status"><option value="all">모든 상태</option><option value="input">입력 자료</option><option value="proposal">AI 제안</option><option value="accepted">채택한 설정</option><option value="deferred">보류한 제안</option></select>
    </div>
    <div class="graph-options">
      <label><input v-model="relations" type="checkbox" /> 관계 표시</label>
      <label :class="{ muted: !selectedId }"><input v-model="onlyNeighbors" type="checkbox" :disabled="!selectedId" /> 선택 항목의 직접 이웃만</label>
    </div>
    <div class="graph-canvas-wrap">
      <div ref="host" class="graph-canvas" role="img" aria-label="설정 관계 그래프. 아래 항목·관계 목록으로 키보드 탐색할 수 있습니다." />
      <div v-if="!visibleNodes.length" class="graph-empty"><span class="empty-orbit">◇</span><strong>{{ graph.nodes.length ? '조건에 맞는 항목이 없어요' : '아직 연결된 설정이 없어요' }}</strong><p>{{ graph.nodes.length ? '검색어나 종류 필터를 바꿔보세요.' : '본문은 그대로 읽을 수 있습니다. 관계가 없는 입력도 정상입니다.' }}</p></div>
      <div class="graph-controls"><button aria-label="그래프 축소" title="축소" @click="zoom(0.8)">−</button><button aria-label="그래프 확대" title="확대" @click="zoom(1.25)">+</button><button aria-label="그래프 화면에 맞춤" @click="cy?.fit(undefined, 45)">맞춤</button></div>
      <div class="graph-legend"><span><i class="legend-solid" />문서에 명시</span><span><i class="legend-dash" />AI 추론</span></div>
    </div>
    <div class="graph-summary"><span>항목 {{ visibleNodes.length }} / 전체 {{ graph.nodes.length }} · 관계 {{ visibleEdges.length }} / 전체 {{ graph.edges.length }}</span><span>탐색은 AI를 호출하지 않습니다</span></div>
    <p v-if="limited" class="notice warning">표시 한도는 30개 항목·60개 관계입니다. 원본은 모두 보존됩니다. 종류·검색·이웃 필터로 범위를 좁혀주세요.</p>
    <p v-for="warning in graph.warnings" :key="warning" class="notice warning">그래프 부분 표시 · {{ warning }}</p>
    <p v-if="issueId" class="graph-note">주황색 강조는 선택한 쟁점과 근거 문단이 겹치는 항목·관계입니다. 문단 전체가 연결되어 실제 범위보다 넓을 수 있습니다.</p>
    <details class="graph-list" open>
      <summary>항목·관계 목록 <span>키보드로도 탐색할 수 있어요</span></summary>
      <div class="graph-list-items">
        <button v-for="item in list" :key="item.data.id" :class="{ chosen: item.data.id === selectedId, 'has-issue': issueMatches(item.data) }" @click="choose(item.data)">
          <span class="list-icon">{{ item.data.source ? '↗' : '◇' }}</span><span class="list-item-title">{{ item.data.label }}<small>{{ item.data.source ? '관계' : kindLabel[item.data.type] || item.data.type }} · {{ statusLabel[item.data.status] || item.data.status }}</small></span><span class="list-basis">{{ basisLabel(item.data.basis) }}</span>
        </button>
        <p v-if="!list.length" class="muted">표시할 항목이나 관계가 없습니다.</p>
      </div>
    </details>
  </div>
</template>

<style scoped>
.graph-view{display:flex;flex-direction:column;min-height:0;padding:20px 24px}.graph-filters{display:flex;gap:8px}.graph-filters input{min-width:80px;flex:1}.graph-filters select{max-width:130px}.graph-options{display:flex;gap:18px;margin:13px 0;font-size:11px;color:var(--muted)}.graph-options label{display:flex;align-items:center;gap:6px}.graph-canvas-wrap{position:relative;border:1px solid var(--line);background:radial-gradient(ellipse at center,#1b2520 0,#151d18 70%);border-radius:8px;overflow:hidden;min-height:370px}.graph-canvas{height:400px;width:100%}.graph-canvas-wrap:before{content:'';pointer-events:none;position:absolute;inset:0;background-image:radial-gradient(#53634f40 .8px,transparent .8px);background-size:21px 21px}.graph-controls{position:absolute;bottom:15px;right:14px;display:flex;gap:2px}.graph-controls button{padding:6px 10px;background:#202a23;min-width:32px}.graph-legend{display:flex;gap:12px;position:absolute;bottom:20px;left:15px;font-size:10px;color:var(--muted)}.graph-legend span{display:flex;gap:5px;align-items:center}.graph-legend i{display:block;width:16px;border-top:1px solid #a1b5a4}.graph-legend .legend-dash{border-top-style:dashed}.graph-summary{display:flex;justify-content:space-between;gap:8px;font-size:10px;color:var(--muted);margin:10px 0 0}.graph-note{font-size:11px;color:var(--gold);line-height:1.7}.graph-empty{position:absolute;top:40%;left:20px;right:20px;text-align:center;pointer-events:none;transform:translateY(-25%)}.graph-empty strong{display:block;font-weight:500;font-size:14px}.graph-empty p{color:var(--muted);font-size:12px}.empty-orbit{display:block;font-size:35px;color:#7d967c;margin-bottom:15px}.graph-list{margin-top:18px;border-top:1px solid var(--line);padding-top:14px}.graph-list summary{cursor:pointer;font-size:12px;color:var(--text)}.graph-list summary span{font-size:10px;color:var(--muted);margin-left:9px}.graph-list-items{max-height:210px;overflow:auto;display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-top:12px}.graph-list-items button{display:flex;align-items:center;gap:8px;text-align:left;min-width:0;padding:9px;border-color:transparent;background:#1e2620}.graph-list-items button.chosen{border-color:var(--gold)}.graph-list-items button.has-issue{box-shadow:inset 2px 0 var(--warning)}.list-icon{font-size:16px;color:var(--gold)}.list-item-title{font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}.list-item-title small{display:block;font-size:9px;color:var(--muted);margin-top:4px}.list-basis{font-size:9px;color:var(--muted)}
@media(max-width:600px){.graph-view{padding:16px}.graph-filters{flex-wrap:wrap}.graph-filters input{flex-basis:100%}.graph-filters select{flex:1;max-width:none}.graph-summary{flex-direction:column}.graph-list-items{grid-template-columns:1fr}.graph-options{font-size:10px;gap:10px}.graph-canvas{height:350px}.graph-legend{bottom:62px}.graph-list summary span{display:block;margin:8px 0}.graph-canvas-wrap{min-height:350px}}
</style>
