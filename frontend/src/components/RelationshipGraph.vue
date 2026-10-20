<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import cytoscape from 'cytoscape'
import type { Core, ElementDefinition } from 'cytoscape'
import type { Evidence, GraphData, GraphItem, GraphResponse } from '../types'
import { basisLabel, graphForDocument, kindLabel, statusLabel } from '../types'
import { graphTopology, isolatedItemPositions, relationshipLayout } from '../graph-layout'

const props = defineProps<{ graph: GraphResponse; currentDocumentRef?: string; issueId?: string; issueEvidence?: Evidence[] }>()
const emit = defineEmits<{ select: [data: GraphData] }>()
const host = ref<HTMLDivElement>()
const expandedDialog = ref<HTMLDialogElement>()
const expanded = ref(false)
let expandTrigger: HTMLElement | undefined
const type = ref('all')
const status = ref('all')
const query = ref('')
const relations = ref(true)
const includeContext = ref(false)
const showEdgeLabels = ref(false)
const documentGraph = computed(() => graphForDocument(props.graph, props.currentDocumentRef, includeContext.value))
const onlyNeighbors = ref(false)
const selectedId = ref('')
const layoutMode = ref<'overview' | 'focus'>('overview')
const componentCount = ref(0)
const isolatedCount = ref(0)
const canvasHeight = ref(470)
let cy: Core | undefined
let observer: ResizeObserver | undefined

const filteredNodes = computed(() => documentGraph.value.nodes.filter(({ data }) => {
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
  return relations.value ? documentGraph.value.edges.filter(x => ids.has(x.data.source || '') && ids.has(x.data.target || '')) : []
})
const visibleEdges = computed(() => scopedEdges.value.slice(0, 60))
const limited = computed(() => scopedNodes.value.length > 30 || scopedEdges.value.length > 60)
const list = computed(() => [...visibleNodes.value, ...visibleEdges.value])
const selectedItem = computed(() => list.value.find(item => item.data.id === selectedId.value)?.data)
const canFocus = computed(() => Boolean(selectedItem.value))

function issueMatches(data: GraphData) {
  if (!props.issueId) return false
  return data.issue_ids?.includes(props.issueId) || data.evidence?.some(evidence =>
    props.issueEvidence?.some(other => other.document_ref === (evidence.document_ref === 'self' ? data.document_ref : evidence.document_ref)
      && other.passage_id === evidence.passage_id))
}
function highlight() {
  if (!cy) return
  cy.elements().removeClass('selected-node neighbor dim issue-match')
  cy.edges().toggleClass('show-label', showEdgeLabels.value)
  cy.edges().forEach(edge => {
    const source = edge.source().position(), target = edge.target().position()
    // Vertical relations need room below the upper node's name.
    edge.style('text-margin-y', Math.abs(target.y - source.y) > 2 * Math.abs(target.x - source.x) ? 8 : -6)
  })
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
  if (layoutMode.value === 'focus') nextTick(arrange)
}
function arrange() {
  if (!cy || !host.value || !visibleNodes.value.length) return
  cy.resize()
  const ids = new Set(visibleNodes.value.map(item => item.data.id))
  const edges = documentGraph.value.edges.filter(item => ids.has(item.data.source || '') && ids.has(item.data.target || '')).slice(0, 60)
  const topology = graphTopology(visibleNodes.value, edges, selectedId.value)
  componentCount.value = topology.components.length
  if (layoutMode.value === 'overview') cy.nodes().positions(node => topology.positions.get(node.id())!)
  // Hide only the lines: placement still follows the same relationships.
  const hiddenEdges = !relations.value ? cy.add(edges.map(item => ({ data: { ...item.data }, classes: 'layout-only' })) as ElementDefinition[]) : undefined
  cy.layout(relationshipLayout(topology, layoutMode.value === 'focus')).run()
  hiddenEdges?.remove()
  const isolatedIds = new Set(topology.components.filter(group => group.length === 1).flat())
  isolatedCount.value = isolatedIds.size
  const isolated = cy.nodes().filter(node => isolatedIds.has(node.id()))
  const connected = cy.nodes().difference(isolated)
  const availableHeight = expanded.value ? expandedCanvasHeight() : canvasHeight.value
  if (expanded.value && host.value.clientWidth > availableHeight * 1.15 && connected.length) {
    const box = connected.boundingBox({ includeOverlays: false, includeUnderlays: false })
    if (box.h > box.w) {
      const originalPositions = new Map(connected.nodes().map(node => [node.id(), { ...node.position() }] as const))
      const center = { x: box.x1 + box.w / 2, y: box.y1 + box.h / 2 }
      connected.forEach(node => {
        const position = node.position()
        node.position({ x: center.x - (position.y - center.y), y: center.y + (position.x - center.x) })
      })
      const rotatedBoxes = connected.map(node => node.boundingBox({ includeOverlays: false, includeUnderlays: false }))
      const hasOverlap = rotatedBoxes.some((a, index) => rotatedBoxes.slice(index + 1).some(b =>
        a.x1 < b.x2 + 6 && a.x2 + 6 > b.x1 && a.y1 < b.y2 + 6 && a.y2 + 6 > b.y1))
      // Labels stay upright. Restore the safe force arrangement if rotating
      // node centres would collide with their unrotated label rectangles.
      if (hasOverlap) connected.positions(node => originalPositions.get(node.id())!)
    }
  }
  const boxes = isolated.map(node => {
    const box = node.boundingBox({ includeOverlays: false, includeUnderlays: false })
    return { id: node.id(), width: box.w, height: box.h }
  })
  const positions = isolatedItemPositions(boxes, connected.length ? connected.boundingBox() : undefined, host.value.clientWidth)
  isolated.forEach(node => {
    const box = node.boundingBox({ includeOverlays: false, includeUnderlays: false })
    const center = positions.get(node.id())!
    node.position({ x: node.position('x') + center.x - (box.x1 + box.w / 2), y: node.position('y') + center.y - (box.y1 + box.h / 2) })
  })
  const bounds = cy.elements().boundingBox({ includeOverlays: false, includeUnderlays: false })
  const widthScale = Math.min(1, Math.max(1, host.value.clientWidth - 80) / Math.max(1, bounds.w))
  canvasHeight.value = expanded.value ? availableHeight : Math.max(host.value.clientWidth < 500 ? 400 : 470, Math.min(720, Math.ceil(bounds.h * widthScale + 80)))
  nextTick(fit)
  highlight()
}
function setLayout(mode: 'overview' | 'focus') {
  layoutMode.value = mode
  arrange()
}
function render() {
  if (!host.value || !cy) return
  if (layoutMode.value === 'focus' && !canFocus.value) layoutMode.value = 'overview'
  const elements: GraphItem[] = [...visibleNodes.value, ...visibleEdges.value]
  cy.elements().remove()
  cy.add(elements.map(item => ({ data: { ...item.data, display_label: item.data.label + (props.currentDocumentRef && item.data.document_ref !== props.currentDocumentRef ? '\n[기준 문서]' : includeContext.value ? '\n[현재 문서]' : '') } })) as ElementDefinition[])
  if (!visibleNodes.value.length) componentCount.value = 0
  arrange()
  highlight()
}
async function toggleExpanded() {
  if (expanded.value) {
    expandedDialog.value?.close()
    return
  }
  expandTrigger = document.activeElement instanceof HTMLElement ? document.activeElement : undefined
  expanded.value = true
  await nextTick()
  expandedDialog.value?.showModal()
  if (expandedDialog.value) expandedDialog.value.scrollTop = 0
  await nextTick()
  arrange()
}
async function closeExpanded() {
  expanded.value = false
  await nextTick()
  arrange()
  expandTrigger?.focus()
}
function expandedCanvasHeight() {
  const wrap = host.value?.parentElement
  const view = wrap?.parentElement
  if (!wrap || !view) return 470
  const children = Array.from(view.children)
  const afterCanvas = children.slice(children.indexOf(wrap) + 1)
  let footerHeight = parseFloat(getComputedStyle(view).paddingBottom) || 0
  for (const child of afterCanvas) {
    const style = getComputedStyle(child)
    if (style.display === 'none') continue
    // Keep the list's summary visible; its expanded rows may scroll below.
    const summary = child.tagName === 'DETAILS' ? child.querySelector('summary') : undefined
    footerHeight += summary ? summary.getBoundingClientRect().height + (parseFloat(style.paddingTop) || 0)
      + (parseFloat(style.paddingBottom) || 0) + (parseFloat(style.borderTopWidth) || 0)
      : child.getBoundingClientRect().height
    footerHeight += (parseFloat(style.marginTop) || 0) + (parseFloat(style.marginBottom) || 0)
  }
  const headerHeight = wrap.getBoundingClientRect().top - view.getBoundingClientRect().top
  return Math.max(260, Math.floor(window.innerHeight - 34 - headerHeight - footerHeight))
}
function fit() {
  if (!cy) return
  if (expanded.value) canvasHeight.value = expandedCanvasHeight()
  cy.resize()
  cy.fit(cy.elements(), 40)
}
function zoom(factor: number) {
  if (!cy) return
  cy.zoom({ level: Math.min(2.5, Math.max(0.05, cy.zoom() * factor)), renderedPosition: { x: cy.width() / 2, y: cy.height() / 2 } })
}

onMounted(() => {
  cy = cytoscape({
    container: host.value,
    minZoom: 0.05,
    maxZoom: 2.5,
    wheelSensitivity: 0.2,
    style: [
      { selector: 'node', style: { label: 'data(display_label)', 'background-color': '#8DAFCF', color: '#172B45', 'font-family': 'system-ui, sans-serif', 'font-size': 14, 'text-valign': 'bottom', 'text-margin-y': 10, width: 27, height: 27, 'border-width': 1.5, 'border-color': '#526F90', 'text-wrap': 'wrap', 'text-max-width': '112px', 'text-overflow-wrap': 'anywhere', 'text-background-color': '#F8FAFC', 'text-background-opacity': 0.94, 'text-background-padding': '3px', 'overlay-opacity': 0 } },
      { selector: 'node[type="person"]', style: { 'background-color': '#82ADE0', 'border-color': '#526F90' } },
      { selector: 'node[type="place"]', style: { shape: 'round-rectangle', 'background-color': '#A6C5D7' } },
      { selector: 'node[type="group"]', style: { shape: 'hexagon', 'background-color': '#9DADD0' } },
      { selector: 'node[type="event"]', style: { shape: 'diamond', 'background-color': '#7998C4', width: 34, height: 34 } },
      { selector: 'node[type="rule"]', style: { shape: 'round-triangle', 'background-color': '#AAC2E4', width: 32, height: 32 } },
      { selector: 'edge', style: { label: '', width: 1.2, 'curve-style': 'bezier', 'line-color': '#7188A3', 'target-arrow-color': '#526F90', 'target-arrow-shape': 'triangle', 'arrow-scale': 0.8, color: '#36516F', 'font-size': 9, 'text-wrap': 'wrap', 'text-max-width': '48px', 'text-overflow-wrap': 'anywhere', 'text-rotation': 0, 'text-background-color': '#F8FAFC', 'text-background-opacity': 0.9, 'text-background-padding': '3px', 'text-margin-y': -6, 'overlay-opacity': 0 } },
      { selector: '.layout-only', style: { visibility: 'hidden' } },
      { selector: 'edge.show-label, edge.hover-label, edge.selected-node', style: { label: 'data(label)' } },
      { selector: '[basis="inferred"]', style: { 'line-style': 'dashed', 'border-style': 'dashed' } },
      { selector: 'node[status="accepted"]', style: { 'border-width': 3, 'border-style': 'double' } },
      { selector: '.dim', style: { opacity: 0.22 } },
      { selector: '.neighbor', style: { opacity: 1 } },
      { selector: '.issue-match', style: { 'border-color': '#A66A14', 'border-width': 4, 'line-color': '#A66A14', 'target-arrow-color': '#A66A14', 'underlay-color': '#A66A14', 'underlay-opacity': 0.14, 'underlay-padding': 8, opacity: 1 } },
      { selector: '.selected-node', style: { 'border-color': '#0F52BA', 'border-width': 4, 'line-color': '#0F52BA', 'target-arrow-color': '#0F52BA', 'underlay-color': '#0F52BA', 'underlay-opacity': 0.16, 'underlay-padding': 10, opacity: 1 } },
    ],
  })
  cy.on('mouseover', 'edge', event => event.target.addClass('hover-label'))
  cy.on('mouseout', 'edge', event => event.target.removeClass('hover-label'))
  cy.on('tap', 'node, edge', event => choose(event.target.data() as GraphData))
  cy.on('tap', event => {
    if (event.target !== cy) return
    const restoreOverview = layoutMode.value === 'focus'
    selectedId.value = ''
    layoutMode.value = 'overview'
    highlight()
    if (restoreOverview) nextTick(arrange)
  })
  observer = new ResizeObserver(() => { fit() })
  if (host.value) observer.observe(host.value)
  render()
})
watch(showEdgeLabels, highlight)
watch(() => props.currentDocumentRef, () => { includeContext.value = false })
watch([visibleNodes, visibleEdges], () => nextTick(render))
watch(() => [props.issueId, props.issueEvidence], highlight, { deep: true })
watch(() => props.graph, () => { selectedId.value = ''; onlyNeighbors.value = false; layoutMode.value = 'overview' })
onBeforeUnmount(() => { observer?.disconnect(); cy?.destroy() })
</script>

<template>
  <dialog ref="expandedDialog" class="graph-expanded-dialog" aria-label="설정 관계 그래프 크게 보기" @close="closeExpanded" />
  <Teleport :to="expandedDialog || 'body'" :disabled="!expanded">
  <div class="graph-view" :class="{ 'graph-view-expanded': expanded }">
    <div class="graph-view-actions"><button :aria-expanded="expanded" @click="toggleExpanded">{{ expanded ? '닫기 · Esc' : '크게 보기 ↗' }}</button></div>
    <p v-if="expanded" class="graph-expanded-help">항목이나 관계를 선택한 뒤 닫으면 「근거와 나의 판단」에서 상세 근거를 볼 수 있어요.<span v-if="selectedItem" role="status">현재 선택: {{ selectedItem.label }}</span></p>
    <div class="graph-filters">
      <label class="sr-only" for="graph-search">그래프 항목 검색</label>
      <input id="graph-search" v-model="query" type="search" placeholder="항목 찾기" />
      <label class="sr-only" for="graph-type">항목 종류</label>
      <select id="graph-type" v-model="type"><option value="all">모든 종류</option><option v-for="(label, value) in kindLabel" :key="value" :value="value">{{ label }}</option></select>
      <label class="sr-only" for="graph-status">설정 상태</label>
      <select id="graph-status" v-model="status"><option value="all">모든 상태</option><option value="input">입력 자료</option><option value="proposal">AI 제안</option><option value="accepted">채택한 설정</option><option value="deferred">보류한 제안</option></select>
    </div>
    <div class="graph-options">
      <label v-if="currentDocumentRef"><input v-model="includeContext" type="checkbox" /> 원문·기준 문서 함께 보기</label>
      <label><input v-model="showEdgeLabels" type="checkbox" /> 관계 이름 모두 표시</label>
      <label><input v-model="relations" type="checkbox" /> 관계 표시</label>
      <label :class="{ muted: !selectedId }"><input v-model="onlyNeighbors" type="checkbox" :disabled="!selectedId" /> 선택 항목과 바로 연결된 항목만</label>
    </div>
    <div class="graph-layout-bar">
      <div class="graph-layout-switch" role="group" aria-label="그래프 배치 방식">
        <button :class="{ active: layoutMode === 'overview' }" :aria-pressed="layoutMode === 'overview'" @click="setLayout('overview')">관계 묶음</button>
        <button :class="{ active: layoutMode === 'focus' }" :aria-pressed="layoutMode === 'focus'" :disabled="!canFocus" title="항목이나 연결선을 선택한 뒤 사용하세요" @click="setLayout('focus')">선택 중심</button>
      </div>
      <button class="graph-rearrange" :disabled="!visibleNodes.length" @click="arrange">다시 배치</button>
    </div>
    <p v-if="visibleNodes.length" class="graph-layout-caption">{{ layoutMode === 'focus' ? '선택 항목 가까이부터 연결 단계별로 펼쳤어요. 연결이 없는 항목은 아래쪽에 따로 모았어요.' : `서로 연결된 항목을 가까이 모았어요 · ${componentCount - isolatedCount}개 연결 묶음${isolatedCount ? ` · 연결 없는 항목 ${isolatedCount}개는 아래쪽에 표시` : ''}` }}</p>
    <p v-if="graph.nodes.length && !expanded" class="graph-help">현재 문서를 중심으로 표시합니다. 이름이 같아도 다른 문서의 항목은 합치지 않습니다. 연결선의 이름은 선택하거나 마우스를 올리면 보입니다. 인물·설정이나 연결선을 선택하면 「근거와 나의 판단」에서 설명과 원문 근거를 볼 수 있어요.</p>
    <div class="graph-canvas-wrap">
      <div ref="host" class="graph-canvas" :style="{ height: `${canvasHeight}px` }" role="img" aria-label="설정 관계 그래프. 아래 항목·관계 목록으로 키보드 탐색할 수 있습니다." />
      <div v-if="!visibleNodes.length" class="graph-empty"><span class="empty-orbit">◇</span><strong>{{ graph.nodes.length ? '조건에 맞는 항목이 없어요' : '아직 연결된 설정이 없어요' }}</strong><p>{{ graph.nodes.length ? '검색어·종류·상태와 연결 항목 필터를 확인해주세요.' : '본문은 그대로 읽을 수 있습니다. 관계가 없는 입력도 정상입니다.' }}</p></div>
    </div>
    <div class="graph-tools">
      <div class="graph-controls"><button aria-label="그래프 축소" title="축소" @click="zoom(0.8)">−</button><button aria-label="그래프 확대" title="확대" @click="zoom(1.25)">+</button><button aria-label="그래프 화면에 맞춤" @click="fit">맞춤</button></div>
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
          <span class="list-icon">{{ item.data.source ? '↗' : '◇' }}</span><span class="list-item-title">{{ item.data.label }}<small>{{ item.data.source ? '관계' : kindLabel[item.data.type] || item.data.type }} · {{ statusLabel[item.data.status] || item.data.status }}{{ currentDocumentRef && item.data.document_ref !== currentDocumentRef ? ' · 기준 문서' : includeContext ? ' · 현재 문서' : '' }}</small></span><span class="list-basis">{{ basisLabel(item.data.basis) }}</span>
        </button>
        <p v-if="!list.length" class="muted">표시할 항목이나 관계가 없습니다.</p>
      </div>
    </details>
  </div>
  </Teleport>
</template>

<style scoped>
.graph-expanded-help{margin:0 0 14px;font-size:12px;line-height:1.8;color:var(--muted)}.graph-expanded-help span{display:block;color:var(--accent);margin-top:4px}.graph-expanded-dialog{width:min(1440px,calc(100vw - 32px));max-width:none;max-height:calc(100dvh - 32px);padding:0;border:1px solid var(--line);border-radius:12px;background:#F8FAFC;color:var(--text);overflow:auto}.graph-expanded-dialog[open]{height:calc(100dvh - 32px)}.graph-expanded-dialog::backdrop{background:#F8FAFCd9}.graph-view-actions{display:flex;justify-content:flex-end;margin:0 0 10px}.graph-view-actions button{font-size:12px;padding:7px 11px;color:var(--text);border-color:var(--line);background:#EEF4FA}.graph-view-expanded{width:100%;box-sizing:border-box}.graph-view-expanded .graph-canvas-wrap{flex-shrink:0}
.graph-layout-bar{display:flex;justify-content:space-between;align-items:center;gap:10px;margin:0 0 9px}.graph-layout-switch{display:flex;gap:3px;padding:3px;border:1px solid var(--line);border-radius:7px;background:#F8FAFC}.graph-layout-switch button,.graph-rearrange{font-size:12px;padding:7px 11px;background:transparent;border-color:transparent}.graph-layout-switch button.active{background:#D6E6F3;border-color:#526A84;color:#172B45}.graph-layout-switch button:disabled{opacity:.4}.graph-rearrange{border-color:var(--line);color:var(--muted)}.graph-layout-caption{margin:0 0 10px;font-size:12px;line-height:1.7;color:#172B45}.graph-help{font-size:12px;line-height:1.8;color:var(--muted);margin:0 0 12px}.graph-view{display:flex;flex-direction:column;min-height:0;padding:20px 24px}.graph-filters{display:flex;gap:8px}.graph-filters input{min-width:80px;flex:1}.graph-filters select{max-width:130px}.graph-options{display:flex;flex-wrap:wrap;gap:12px;margin:13px 0;font-size:12px;color:var(--muted)}.graph-options label{display:flex;align-items:center;gap:6px}.graph-canvas-wrap{position:relative;border:1px solid var(--line);background:radial-gradient(ellipse at center,#EEF4FA 0,#F8FAFC 70%);border-radius:8px;overflow:hidden;min-height:370px}.graph-canvas{height:470px;width:100%}.graph-canvas-wrap:before{content:'';pointer-events:none;position:absolute;inset:0;background-image:radial-gradient(#A6C5D740 .8px,transparent .8px);background-size:21px 21px}.graph-tools{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-top:8px}.graph-controls{display:flex;gap:2px}.graph-controls button{padding:6px 10px;background:#EEF4FA;min-width:32px}.graph-legend{display:flex;gap:12px;font-size:12px;color:var(--muted)}.graph-legend span{display:flex;gap:5px;align-items:center}.graph-legend i{display:block;width:16px;border-top:1px solid #36516F}.graph-legend .legend-dash{border-top-style:dashed}.graph-summary{display:flex;justify-content:space-between;gap:8px;font-size:12px;color:var(--muted);margin:10px 0 0}.graph-note{font-size:12px;color:var(--accent);line-height:1.7}.graph-empty{position:absolute;top:40%;left:20px;right:20px;text-align:center;pointer-events:none;transform:translateY(-25%)}.graph-empty strong{display:block;font-weight:500;font-size:14px}.graph-empty p{color:var(--muted);font-size:12px}.empty-orbit{display:block;font-size:35px;color:#526A84;margin-bottom:15px}.graph-list{margin-top:18px;border-top:1px solid var(--line);padding-top:14px}.graph-list summary{cursor:pointer;font-size:12px;color:var(--text)}.graph-list summary span{font-size:12px;color:var(--muted);margin-left:9px}.graph-list-items{max-height:210px;overflow:auto;display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-top:12px}.graph-list-items button{display:flex;align-items:center;gap:8px;text-align:left;min-width:0;padding:9px;border-color:transparent;background:#EEF4FA}.graph-list-items button.chosen{border-color:var(--accent)}.graph-list-items button.has-issue{box-shadow:inset 2px 0 var(--warning)}.list-icon{font-size:16px;color:var(--accent)}.list-item-title{font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}.list-item-title small{display:block;font-size:12px;color:var(--muted);margin-top:4px}.list-basis{font-size:12px;color:var(--muted)}
@media(max-width:600px){.graph-view{padding:16px}.graph-filters{flex-wrap:wrap}.graph-filters input{flex-basis:100%}.graph-filters select{flex:1;max-width:none}.graph-summary{flex-direction:column}.graph-list-items{grid-template-columns:1fr}.graph-options{font-size:12px;gap:10px}.graph-canvas{height:350px}.graph-list summary span{display:block;margin:8px 0}.graph-canvas-wrap{min-height:350px}}
/* Keep canvas labels and controls consistent with the document palette. */
.graph-canvas-wrap{background:#F8FAFC}.graph-canvas-wrap:before{background-image:radial-gradient(#A6C5D766 .8px,transparent .8px)}
.graph-expanded-dialog{background:#F8FAFC}.graph-expanded-dialog::backdrop{background:#000926CC}
.graph-layout-switch button.active{background:#D6E6F3;border-color:#0F52BA;color:#0F52BA}
.graph-layout-caption{color:#526A84}.graph-controls button,.graph-view-actions button{background:#EEF4FA}
.graph-list-items button{background:#EEF4FA}.graph-list-items button.chosen{background:#D6E6F3;border-color:#0F52BA}
.graph-note{color:var(--warning)}.graph-expanded-help span{color:var(--accent)}
</style>
