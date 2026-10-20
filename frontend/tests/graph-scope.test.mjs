import assert from 'node:assert/strict'
import test from 'node:test'
import * as graphTools from '../src/types.ts'
const { graphCacheKey } = graphTools
const node = (id, document_ref, label) => ({ data: { id, document_ref, label } })
const graph = {
  nodes: [node('old-rule','input:1','체질'), node('new-rule','draft:1','체질'), node('new-person','draft:1','인물'), node('old-place','input:1','장소')],
  edges: [{ data: { id:'r1', document_ref:'draft:1', source:'new-person', target:'new-rule' } }, { data: { id:'r2', document_ref:'draft:1', source:'new-person', target:'old-place' } }],
  warnings: []
}
test('current document excludes unrelated baseline twins but keeps referenced endpoints', () => {
  const visible = graphTools.graphForDocument(graph, 'draft:1', false)
  assert.deepEqual(visible.nodes.map(n => n.data.id), ['new-rule','new-person','old-place'])
  assert.equal(visible.edges.length, 2)
  assert.equal(graph.nodes.length, 4)
})
test('explicit combined view preserves distinct identities; names never merge', () => {
  assert.equal(graphTools.graphForDocument(graph, 'draft:1', true).nodes.length, 4)
  assert.equal(graphTools.graphForDocument(graph, 'missing:1', false).nodes.length, 0)
  assert.equal(graphTools.graphForDocument(graph, undefined, false).nodes.length, 4)
})


test('completed reviews invalidate the graph cached before findings existed', () => {
  const run = { id: 'review1', status: 'running', reviews: [], decision: { status: 'pending' } }
  const pending = graphCacheKey(run, 'input')
  const reviewed = { ...run, reviews: [{ issues: [{ id: 'conflict' }] }] }
  assert.notEqual(graphCacheKey(reviewed, 'input'), pending)
  assert.notEqual(graphCacheKey({ ...reviewed, status: 'needs_attention' }, 'input'), graphCacheKey(reviewed, 'input'))
  assert.equal(graphCacheKey(reviewed, 'input'), graphCacheKey({ ...reviewed }, 'input'))
})
