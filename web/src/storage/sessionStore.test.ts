import { describe, expect, it } from 'vitest'

import type { SessionCheckpoint } from '../domain/types'
import { createCheckpointStore } from './sessionStore'

function memoryStorage(initial: Record<string, string> = {}): Storage {
  const values = new Map(Object.entries(initial))
  return {
    get length() {
      return values.size
    },
    clear: () => values.clear(),
    getItem: (key) => values.get(key) ?? null,
    key: (index) => [...values.keys()][index] ?? null,
    removeItem: (key) => values.delete(key),
    setItem: (key, value) => values.set(key, value),
  }
}

describe('checkpointStore', () => {
  it('defers Storage access errors until a checkpoint operation', () => {
    const error = new DOMException('Storage is disabled', 'SecurityError')
    const store = createCheckpointStore(() => {
      throw error
    })

    expect(() => store.load('one')).toThrow(error)
  })

  it.each([
    null,
    {},
    { items: [null], appliedEventIds: [] },
    { items: [{ kind: 'unknown', key: 'x', createdAt: '' }], appliedEventIds: [] },
    { items: [], appliedEventIds: [3] },
    { items: [], appliedEventIds: [], resumeToken: {} },
    { items: [], appliedEventIds: [], terminal: { status: 'running' } },
    { items: [], appliedEventIds: [], terminal: { status: 'failed', error: [] } },
    {
      items: [],
      appliedEventIds: [],
      terminal: { status: 'failed', error: { message: {} } },
    },
    { items: [], appliedEventIds: [], terminal: { status: 'succeeded', usage: {} } },
    {
      items: [],
      appliedEventIds: [],
      terminal: { status: 'succeeded', timing: { elapsed_ms: '10', active_ms: 5 } },
    },
    ...[
      { kind: 'block', blockType: 'text', content: {}, completed: true },
      { kind: 'block', blockType: 'text', content: '', completed: true, messageId: {} },
      { kind: 'tool', name: 'search', status: 'completed', arguments: [] },
      { kind: 'tool', name: 'search', status: 'completed', content: {} },
      { kind: 'status', label: 'Done', tone: 'unknown' },
      { kind: 'compaction', status: 'completed', beforeTokens: '100' },
      { kind: 'message-boundary', messageId: null },
    ].map((item) => ({
      items: [{ key: 'item', createdAt: '', ...item }],
      appliedEventIds: [],
    })),
  ])('rejects a malformed cached projection: %j', (timeline) => {
    const storage = memoryStorage({
      'agentsystem.checkpoint.one': JSON.stringify({
        cursor: 'epoch:1-0',
        appliedEventIds: [],
        timeline,
      }),
    })

    expect(createCheckpointStore(storage).load('one')).toBeNull()
  })

  it('roundtrips all supported timeline items and terminal metadata', () => {
    const checkpoint: SessionCheckpoint = {
      cursor: 'epoch:1-0',
      appliedEventIds: ['event'],
      timeline: {
        cursor: 'epoch:1-0',
        appliedEventIds: ['event'],
        resumeToken: 'token',
        resumeExpiresAt: 'later',
        items: [
          {
            kind: 'block',
            key: 'b',
            createdAt: '',
            blockType: 'text',
            content: 'Hello',
            completed: true,
            index: 0,
            durationMs: 1,
          },
          {
            kind: 'tool',
            key: 't',
            createdAt: '',
            name: 'search',
            status: 'completed',
            arguments: { query: ['a'] },
            content: 'result',
            isError: false,
            truncated: true,
          },
          {
            kind: 'status',
            key: 's',
            createdAt: '',
            label: 'Done',
            tone: 'success',
            detail: 'Finished',
          },
          {
            kind: 'compaction',
            key: 'c',
            createdAt: '',
            status: 'completed',
            action: 'pruned',
            beforeTokens: 100,
            afterTokens: 50,
          },
          { kind: 'message-boundary', key: 'm', createdAt: '', messageId: 'message' },
        ],
        terminal: {
          status: 'succeeded',
          usage: {
            input_tokens: 10,
            output_tokens: 5,
            total_tokens: 15,
            reasoning_tokens: null,
          },
          timing: { elapsed_ms: 10, active_ms: 5 },
          error: null,
        },
      },
    }
    const store = createCheckpointStore(memoryStorage())
    store.save('one', checkpoint)

    expect(store.load('one')).toEqual(checkpoint)
  })

  it('isolates checkpoints by session and clears only the requested one', () => {
    const store = createCheckpointStore(memoryStorage())
    store.save('one', { cursor: 'epoch:1-0', appliedEventIds: ['event-1'] })
    store.save('two', { cursor: 'epoch:2-0', appliedEventIds: [] })

    store.clear('one')

    expect(store.load('one')).toBeNull()
    expect(store.load('two')?.cursor).toBe('epoch:2-0')
  })
})
