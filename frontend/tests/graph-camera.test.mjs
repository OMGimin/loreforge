import assert from 'node:assert/strict'
import test from 'node:test'
import { GRAPH_LABEL_PX, graphMetrics, projectedBounds, readableViewport, viewportPositions } from '../src/graph-camera.ts'

const box = (x, y) => ({ x, y, left: -48, right: 48, top: -15, bottom: 48 })
const overlap = (a, b, z) => a.x*z+a.left < b.x*z+b.right && a.x*z+a.right > b.x*z+b.left
  && a.y*z+a.top < b.y*z+b.bottom && a.y*z+a.bottom > b.y*z+b.top

test('tall layout uses a wide viewport without changing order or label dimensions', () => {
  const boxes = [box(0, 0), box(100, 200), box(50, 800)]
  const adapted = viewportPositions(boxes, 1180, 300)
  const camera = readableViewport(adapted, 1180, 300)
  assert.equal(camera.overflow, false)
  assert.ok(adapted[0].x < adapted[2].x && adapted[2].x < adapted[1].x)
  assert.ok(adapted[0].y < adapted[1].y && adapted[1].y < adapted[2].y)
  assert.deepEqual(adapted.map(b => b.right - b.left), boxes.map(b => b.right - b.left))
})

test('aspect-ratio adaptation separates labels that converge after vertical compression', () => {
  const boxes = [box(0,0),box(800,0),box(800,800),box(0,800),box(390,390),box(400,420),box(420,450)]
  const adapted = viewportPositions(boxes, 1180, 300)
  const camera = readableViewport(adapted, 1180, 300)
  assert.equal(camera.overflow,false)
  for (const [i,a] of adapted.entries()) for (const b of adapted.slice(i+1)) assert.equal(overlap(a,b,camera.zoom),false)
})

test('fit a wide 12-node graph without shrinking labels or losing endpoints', () => {
  const boxes = [box(0,0), ...Array.from({length:11}, (_,i) => box(Math.cos(i*2*Math.PI/11)*760, Math.sin(i*2*Math.PI/11)*360))]
  const camera = readableViewport(boxes, 1180, 430)
  assert.ok(camera.zoom < 1)
  assert.equal(graphMetrics(camera.zoom).font * camera.zoom, GRAPH_LABEL_PX)
  assert.equal(camera.overflow, false)
  const bounds = projectedBounds(boxes, camera.zoom)
  assert.ok(bounds.x1 + camera.pan.x >= 23)
  assert.ok(bounds.y1 + camera.pan.y >= 23)
  assert.ok(bounds.x2 + camera.pan.x <= 1157)
  assert.ok(bounds.y2 + camera.pan.y <= 407)
  for (const [i,a] of boxes.entries()) for (const b of boxes.slice(i+1)) assert.equal(overlap(a,b,camera.zoom),false)
})

test('dense graph keeps readable labels and reports that panning is necessary', () => {
  const boxes = Array.from({length:30}, (_,i) => box(i%6*140,Math.floor(i/6)*100))
  const camera = readableViewport(boxes, 320, 300)
  assert.equal(camera.overflow,true)
  assert.ok(graphMetrics(camera.zoom).font * camera.zoom >= 15.99)
  for (const [i,a] of boxes.entries()) for (const b of boxes.slice(i+1)) assert.equal(overlap(a,b,camera.zoom),false)
})

test('larger viewport can reveal more of a graph without changing text size', () => {
  const boxes = Array.from({length:20}, (_,i)=>box(i%5*170, Math.floor(i/5)*120))
  const small = readableViewport(boxes, 350, 300)
  const large = readableViewport(boxes, 1180, 550)
  assert.equal(small.overflow,true)
  assert.equal(large.overflow,false)
  assert.equal(graphMetrics(small.zoom).font * small.zoom, graphMetrics(large.zoom).font * large.zoom)
})

test('empty and single-node graphs have finite centred cameras', () => {
  for (const boxes of [[],[box(900,-300)]]) {
    const camera=readableViewport(boxes,600,400)
    assert.ok(Number.isFinite(camera.zoom+camera.pan.x+camera.pan.y))
    assert.equal(camera.overflow,false)
  }
})
