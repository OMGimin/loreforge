import type { ConcentricLayoutOptions, CoseLayoutOptions } from 'cytoscape'

interface LayoutNode { data: { id: string; type?: string; label?: string } }
interface LayoutEdge { data: { id?: string; source?: string; target?: string } }

/** Undirected distance is intentional: an arrow describes a relation, not a hierarchy. */
export function graphTopology(nodes: LayoutNode[], edges: LayoutEdge[], selectedId = '') {
  const byId = new Map(nodes.map(node => [node.data.id, node]))
  const adjacency = new Map(nodes.map(node => [node.data.id, new Set<string>()]))
  for (const { data } of edges) {
    if (data.source === data.target || !adjacency.has(data.source || '') || !adjacency.has(data.target || '')) continue
    adjacency.get(data.source!)!.add(data.target!)
    adjacency.get(data.target!)!.add(data.source!)
  }
  const compare = (a: string, b: string) => (byId.get(a)?.data.type || '').localeCompare(byId.get(b)?.data.type || '')
    || (byId.get(a)?.data.label || a).localeCompare(byId.get(b)?.data.label || b) || a.localeCompare(b)
  const unseen = new Set([...byId.keys()].sort(compare))
  const components: string[][] = []
  while (unseen.size) {
    const queue = [unseen.values().next().value!]
    unseen.delete(queue[0]!)
    for (let index = 0; index < queue.length; index++) {
      for (const id of [...adjacency.get(queue[index]!)!].sort(compare)) {
        if (unseen.delete(id)) queue.push(id)
      }
    }
    components.push(queue)
  }
  components.sort((a, b) => b.length - a.length || compare(a[0]!, b[0]!))
  const selectedEdge = edges.find(edge => edge.data.id === selectedId)
  const focusIds = byId.has(selectedId) ? [selectedId]
    : [selectedEdge?.data.source, selectedEdge?.data.target].filter((id): id is string => Boolean(id && byId.has(id)))
  const distances = new Map<string, number>()
  const queue = [...new Set(focusIds)]
  queue.forEach(id => distances.set(id, 0))
  for (let index = 0; index < queue.length; index++) {
    for (const id of adjacency.get(queue[index]!)!) {
      if (!distances.has(id)) {
        distances.set(id, distances.get(queue[index]!)! + 1)
        queue.push(id)
      }
    }
  }
  // Start each connected component at its most connected item. Stable circular
  // seeds keep COSE reproducible and avoid the random pile-up at the origin.
  const positions = new Map<string, { x: number; y: number }>()
  let offset = 0
  for (const component of components) {
    const ordered = [...component].sort((a, b) => adjacency.get(b)!.size - adjacency.get(a)!.size || compare(a, b))
    const root = ordered[0]!
    positions.set(root, { x: offset, y: 0 })
    const rest = ordered.slice(1).sort(compare)
    const radius = Math.max(125, rest.length * 24)
    rest.forEach((id, index) => {
      const angle = (index / rest.length) * Math.PI * 2 - Math.PI / 2
      positions.set(id, { x: offset + Math.cos(angle) * radius, y: Math.sin(angle) * radius })
    })
    offset += radius * 2 + 160
  }
  return { components, focusIds, distances, positions }
}

export function relationshipLayout(topology: ReturnType<typeof graphTopology>, focus: boolean): CoseLayoutOptions | ConcentricLayoutOptions {
  const common = { animate: false, fit: false, padding: 36, nodeDimensionsIncludeLabels: true }
  if (focus && topology.focusIds.length) {
    const maxDistance = Math.max(0, ...topology.distances.values()) + 1
    return {
      ...common, name: 'concentric', avoidOverlap: true, minNodeSpacing: 28,
      concentric: node => maxDistance - (topology.distances.get(node.id()) ?? maxDistance),
      levelWidth: () => 1,
      sort: (a, b) => String(a.data('type')).localeCompare(String(b.data('type'))) || a.id().localeCompare(b.id()),
    }
  }
  return {
    ...common, name: 'cose', randomize: false, componentSpacing: 90,
    nodeRepulsion: () => 12000, nodeOverlap: 24, idealEdgeLength: () => 100,
    edgeElasticity: () => 110, gravity: 0.22, numIter: 900,
    initialTemp: 160, coolingFactor: 0.96, minTemp: 1,
  }
}

interface ItemBox { id: string; width: number; height: number }
interface LayoutBounds { x1: number; y1: number; w: number; h: number }

/** Pack unrelated items into a compact shelf instead of extending the force graph. */
export function isolatedItemPositions(items: ItemBox[], connected: LayoutBounds | undefined, viewportWidth: number) {
  const width = Math.max(connected?.w ?? Math.max(280, viewportWidth - 80), ...items.map(item => item.width))
  const left = connected?.x1 ?? 0
  let x = left
  let y = connected ? connected.y1 + connected.h + 36 : 0
  let rowHeight = 0
  const positions = new Map<string, { x: number; y: number }>()
  for (const item of [...items].sort((a, b) => a.id.localeCompare(b.id))) {
    if (x > left && x + item.width > left + width) {
      x = left
      y += rowHeight + 20
      rowHeight = 0
    }
    positions.set(item.id, { x: x + item.width / 2, y: y + item.height / 2 })
    x += item.width + 24
    rowHeight = Math.max(rowHeight, item.height)
  }
  return positions
}
