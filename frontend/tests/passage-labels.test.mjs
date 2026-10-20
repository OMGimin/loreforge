import assert from 'node:assert/strict'
import test from 'node:test'
import * as labels from '../src/types.ts'

const draft = { kind: 'draft', passages: [{ id: 'p999', text: '첫 내용' }, { id: 'scene-b', text: '다음 내용' }] }
test('reader labels use document order, not opaque identifiers', () => {
  assert.equal(labels.passageLabel(draft, 'p999'), '1번 문단')
  assert.equal(labels.passageLabel(draft, 'scene-b'), '2번 문단')
})
test('evidence labels identify the exact displayed version', () => {
  assert.equal(labels.evidenceLabel(draft, 'scene-b'), '첫 초안 · 2번 문단')
  const revision = { kind: 'revision', passages: [...draft.passages].reverse() }
  assert.equal(labels.evidenceLabel(revision, 'scene-b'), '수정본 · 1번 문단')
  assert.equal(labels.evidenceLabel({ ...draft, kind: 'input' }, 'p999'), '입력 원문 · 1번 문단')
})
test('missing references never invent paragraph numbers', () => {
  assert.equal(labels.passageLabel(draft, 'p001'), '찾을 수 없는 문단')
  assert.equal(labels.evidenceLabel(undefined, 'p001'), '이전 문서의 근거')
})
