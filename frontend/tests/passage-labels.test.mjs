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

test('finding prose translates only cited exact IDs and keeps saved content intact', () => {
  const doc = { ...draft, document_id: 'doc', version: 2 }
  const issue = { evidence: [{ document_ref: 'doc:2', passage_id: 'p999' }] }
  const original = 'p999와 p9999, p001 및 일반 단어는 구분한다.'
  assert.equal(labels.readableIssueText(original, issue, [doc]), '첫 초안 · 1번 문단과 p9999, p001 및 일반 단어는 구분한다.')
  assert.equal(original, 'p999와 p9999, p001 및 일반 단어는 구분한다.')
  assert.equal(labels.readableIssueText(original, issue, [{ ...doc, version: 3 }]), original)
})
