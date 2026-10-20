import assert from 'node:assert/strict'
import test from 'node:test'
import cytoscape from 'cytoscape'
import { graphTopology, isolatedItemPositions, relationshipLayout } from '../src/graph-layout.ts'
const node = (id, type = 'person') => ({ data: { id, label: id, type } })
const edge = (source, target) => ({ data: { id: `${source}-${target}`, source, target } })

test('relationship grouping uses connections, not names or input order', () => {
  const nodes = [node('a'), node('b'), node('c'), node('d'), node('alone')]
  const edges = [edge('a', 'b'), edge('c', 'd'), edge('a', 'missing'), edge('b', 'a')]
  const first = graphTopology(nodes, edges)
  const reordered = graphTopology([...nodes].reverse(), [...edges].reverse())
  assert.deepEqual(first.components, [['a', 'b'], ['c', 'd'], ['alone']])
  assert.deepEqual(first.positions, reordered.positions)
  assert.equal(new Set([...first.positions.values()].map(p => `${p.x},${p.y}`)).size, nodes.length)
})

test('focus rings follow connection distance in either arrow direction', () => {
  const nodes = ['a', 'b', 'c', 'd', 'alone'].map(id => node(id))
  const edges = [edge('a', 'b'), edge('c', 'b'), edge('c', 'd')]
  const topology = graphTopology(nodes, edges, 'b')
  assert.equal(topology.distances.get('b'), 0)
  assert.equal(topology.distances.get('a'), 1)
  assert.equal(topology.distances.get('c'), 1)
  assert.equal(topology.distances.get('d'), 2)
  assert.equal(topology.distances.has('alone'), false)
  const layout = relationshipLayout(topology, true)
  const level = id => layout.concentric({ id: () => id })
  assert.ok(level('b') > level('a'))
  assert.ok(level('a') > level('d'))
  assert.ok(level('d') > level('alone'))
  assert.deepEqual(graphTopology(nodes, edges, 'a-b').focusIds, ['a', 'b'])
})

test('overview spreads a 20-item hub without overlapping readable node boxes', () => {
  const nodes = [node('hub'), ...Array.from({ length: 19 }, (_, i) => node(`n${i}`))]
  const edges = nodes.slice(1).map(n => edge('hub', n.data.id))
  const topology = graphTopology(nodes, edges)
  const cy = cytoscape({ headless: true, styleEnabled: true,
    style: [{ selector: 'node', style: { width: 112, height: 70 } }],
    elements: [...nodes.map(n => ({ ...n, position: topology.positions.get(n.data.id) })), ...edges],
  })
  cy.layout(relationshipLayout(topology, false)).run()
  const all = cy.nodes().toArray()
  for (let a = 0; a < all.length; a++) {
    const p = all[a].position()
    assert.ok(Number.isFinite(p.x) && Number.isFinite(p.y))
    for (let b = a + 1; b < all.length; b++) {
      const q = all[b].position()
      assert.ok(Math.abs(p.x - q.x) >= 112 || Math.abs(p.y - q.y) >= 70, `overlap: ${all[a].id()} / ${all[b].id()}`)
    }
  }
  cy.destroy()
})


test('seven unrelated items form a compact non-overlapping shelf below the connected group', () => {
  const items = Array.from({ length: 7 }, (_, i) => ({ id: `alone${i}`, width: 90, height: 62 }))
  const bounds = { x1: 100, y1: 80, w: 560, h: 380 }
  const positions = isolatedItemPositions(items, bounds, 650)
  for (let index = 0; index < items.length; index++) {
    const position = positions.get(items[index].id)
    assert.ok(position.y - 31 > bounds.y1 + bounds.h)
    assert.ok(position.x - 45 >= bounds.x1)
    assert.ok(position.x + 45 <= bounds.x1 + bounds.w)
    for (const other of items.slice(index + 1)) {
      const next = positions.get(other.id)
      assert.ok(Math.abs(position.x - next.x) >= 90 || Math.abs(position.y - next.y) >= 62)
    }
  }
  assert.ok(Math.max(...positions.values().map(p => p.y + 31)) < bounds.y1 + bounds.h + 200)
  assert.equal(isolatedItemPositions(items, undefined, 650).size, 7)
})
