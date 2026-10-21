/** Label and node sizes are CSS pixels, independent of camera zoom. */
export const GRAPH_LABEL_PX = 16
export interface CameraBox {
  x: number; y: number
  left: number; right: number; top: number; bottom: number
}

export function graphMetrics(zoom: number) {
  const scale = 1 / Math.max(0.001, zoom)
  return { font: GRAPH_LABEL_PX * scale, edgeFont: 12 * scale,
    node: 27 * scale, event: 34 * scale, rule: 32 * scale,
    margin: 10 * scale, wrap: 112 * scale, edgeWrap: 96 * scale, padding: 3 * scale }
}

export function projectedBounds(boxes: CameraBox[], zoom: number) {
  if (!boxes.length) return { x1: 0, x2: 0, y1: 0, y2: 0, w: 0, h: 0 }
  const x1 = Math.min(...boxes.map(b => b.x * zoom + b.left))
  const x2 = Math.max(...boxes.map(b => b.x * zoom + b.right))
  const y1 = Math.min(...boxes.map(b => b.y * zoom + b.top))
  const y2 = Math.max(...boxes.map(b => b.y * zoom + b.bottom))
  return { x1, x2, y1, y2, w: x2 - x1, h: y2 - y1 }
}

/** Use the available aspect ratio without changing connectivity or label size. */
export function viewportPositions(boxes: CameraBox[], width: number, height: number, padding = 24) {
  if (!boxes.length) return []
  const minX = Math.min(...boxes.map(b => b.x)), maxX = Math.max(...boxes.map(b => b.x))
  const minY = Math.min(...boxes.map(b => b.y)), maxY = Math.max(...boxes.map(b => b.y))
  const left = Math.max(...boxes.map(b => -b.left)), right = Math.max(...boxes.map(b => b.right))
  const top = Math.max(...boxes.map(b => -b.top)), bottom = Math.max(...boxes.map(b => b.bottom))
  const spaceX = Math.max(1, width - 2 * padding - left - right)
  const spaceY = Math.max(1, height - 2 * padding - top - bottom)
  const placed = boxes.map(b => ({ ...b,
    x: maxX === minX ? width / 2 : padding + left + (b.x - minX) / (maxX - minX) * spaceX,
    y: maxY === minY ? height / 2 : padding + top + (b.y - minY) / (maxY - minY) * spaceY,
  }))
  // Fitting a tall force layout into a wide canvas can bring names together.
  // Relax only colliding rectangles, keeping their surrounding layout intact.
  const clamp = (b: CameraBox) => {
    b.x = Math.max(padding - b.left, Math.min(width - padding - b.right, b.x))
    b.y = Math.max(padding - b.top, Math.min(height - padding - b.bottom, b.y))
  }
  for (let round = 0; round < 80; round++) {
    let moved = false
    for (let i = 0; i < placed.length; i++) for (const b of placed.slice(i + 1)) {
      const a = placed[i]!
      const [l, r] = a.x <= b.x ? [a, b] : [b, a]
      const [t, d] = a.y <= b.y ? [a, b] : [b, a]
      const dx = l.x + l.right + 12 - (r.x + r.left)
      const dy = t.y + t.bottom + 12 - (d.y + d.top)
      if (dx <= 0 || dy <= 0) continue
      if (dx < dy) { l.x -= dx / 2; r.x += dx / 2 }
      else { t.y -= dy / 2; d.y += dy / 2 }
      clamp(a); clamp(b)
      moved = true
    }
    if (!moved) break
  }
  return placed
}

/** Minimum separation for upright, fixed-size labels, including asymmetric offsets. */
export function minimumReadableZoom(boxes: CameraBox[], gap = 10) {
  let minimum = 0.05
  for (let i = 0; i < boxes.length; i++) {
    const a = boxes[i]!
    for (const b of boxes.slice(i + 1)) {
      const [left, right] = a.x < b.x ? [a, b] : [b, a]
      const [top, bottom] = a.y < b.y ? [a, b] : [b, a]
      const horizontal = right.x === left.x ? Infinity : (left.right - right.left + gap) / (right.x - left.x)
      const vertical = bottom.y === top.y ? Infinity : (top.bottom - bottom.top + gap) / (bottom.y - top.y)
      minimum = Math.max(minimum, Math.min(horizontal, vertical))
    }
  }
  return Number.isFinite(minimum) ? minimum : 1
}

export function readableViewport(boxes: CameraBox[], width: number, height: number, padding = 24) {
  const minimum = minimumReadableZoom(boxes)
  // A small graph need not be stretched; its names are already 16 CSS pixels.
  let low = 0, high = 1
  for (let i = 0; i < 30; i++) {
    const middle = (low + high) / 2
    const bounds = projectedBounds(boxes, middle)
    if (bounds.w <= width - 2 * padding && bounds.h <= height - 2 * padding) low = middle
    else high = middle
  }
  const zoom = Math.max(minimum, low)
  const bounds = projectedBounds(boxes, zoom)
  return { zoom, minZoom: minimum, pan: { x: width / 2 - (bounds.x1 + bounds.x2) / 2,
    y: height / 2 - (bounds.y1 + bounds.y2) / 2 },
    overflow: bounds.w > width - 2 * padding + 1 || bounds.h > height - 2 * padding + 1 }
}
