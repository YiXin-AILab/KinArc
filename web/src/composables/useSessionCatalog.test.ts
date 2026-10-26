import { effectScope } from 'vue'
import { flushPromises } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import type { RuntimeClient } from '../api/client'
import { RuntimeHttpError } from '../api/client'
import type { SessionRecord } from '../domain/types'
import { sessionKey, useSessionCatalog } from './useSessionCatalog'

const first: SessionRecord = {
  sessionId: 'same',
  agentId: 'z',
  title: 'First',
  titleVersion: 1,
  createdAt: '2026-09-24T00:00:00Z',
  updatedAt: '2026-09-24T00:00:00Z',
}
const second: SessionRecord = { ...first, agentId: 'a', title: 'Second' }

/** 不挂载无关消息观察器，直接验证元数据所有权。 */
function setup() {
  const client = {
    sessions: vi
      .fn()
      .mockResolvedValue({ sessions: [first, second], nextCursor: 'page-2' }),
    renameSession: vi
      .fn()
      .mockResolvedValue({ ...first, title: 'Manual', titleVersion: 3 }),
    generateSessionTitle: vi.fn(),
    deleteSession: vi.fn().mockResolvedValue(undefined),
  }
  const scope = effectScope()
  const catalog = scope.run(() =>
    useSessionCatalog(client as unknown as RuntimeClient, new Set(['a', 'z'])),
  )!
  return { scope, client, catalog }
}

describe('server session catalog', () => {
  it('replaces refreshed pages so remotely deleted records do not remain visible', async () => {
    const { scope, client, catalog } = setup()
    try {
      await catalog.load()
      client.sessions.mockResolvedValue({ sessions: [second], nextCursor: null })
      await catalog.load()
      expect(catalog.sessions.value).toEqual([second])
      client.sessions.mockResolvedValue({ sessions: [], nextCursor: null })
      await catalog.load()
      expect(catalog.sessions.value).toEqual([])
    } finally {
      scope.stop()
    }
  })
  it('preserves server page order, deduplicates pages, and deletes only the selected agent identity', async () => {
    const { scope, client, catalog } = setup()
    try {
      await catalog.load()
      expect(catalog.sessions.value.map((item) => item.agentId)).toEqual(['z', 'a'])
      client.sessions.mockResolvedValue({
        sessions: [
          second,
          { ...first, sessionId: 'older', createdAt: '2026-09-23T00:00:00Z' },
        ],
        nextCursor: null,
      })
      await catalog.load(true)
      expect(client.sessions).toHaveBeenLastCalledWith(
        'page-2',
        expect.any(AbortSignal),
      )
      expect(catalog.sessions.value.map(sessionKey)).toEqual([
        '["z","same"]',
        '["a","same"]',
        '["z","older"]',
      ])
      client.sessions.mockResolvedValueOnce({
        sessions: [first],
        nextCursor: 'refreshed-page-2',
      })
      await catalog.load()
      expect(client.sessions).toHaveBeenLastCalledWith(
        'refreshed-page-2',
        expect.any(AbortSignal),
      )
      expect(catalog.sessions.value.map(sessionKey)).toEqual([
        '["z","same"]',
        '["a","same"]',
        '["z","older"]',
      ])
      await catalog.remove(first)
      expect(client.deleteSession).toHaveBeenCalledWith(first, expect.any(AbortSignal))
      expect(catalog.sessions.value.map(sessionKey)).toContain('["a","same"]')
      await catalog.load()
      expect(catalog.sessions.value.map(sessionKey)).not.toContain('["z","same"]')
    } finally {
      scope.stop()
    }
  })

  it('keeps rejected deletions and ignores late title and page responses after a successful retry', async () => {
    const { scope, client, catalog } = setup()
    let finishTitle!: (record: SessionRecord) => void
    client.generateSessionTitle.mockImplementation(
      () =>
        new Promise((resolve) => {
          finishTitle = resolve
        }),
    )
    client.deleteSession.mockRejectedValueOnce(new Error('session is active'))
    try {
      catalog.accepted(first, true)
      catalog.messageAvailable(first)
      await flushPromises()
      expect(await catalog.remove(first)).toBe(false)
      expect(catalog.sessions.value).toContainEqual(first)
      expect(catalog.error.value).toBe('session is active')
      expect(await catalog.remove(first)).toBe(true)
      finishTitle({ ...first, title: 'Late', titleVersion: 2 })
      await catalog.load()
      await flushPromises()
      expect(catalog.sessions.value).toEqual([second])
    } finally {
      scope.stop()
    }
  })

  it('keeps a manual title when a stale list and an in-flight generated title finish later', async () => {
    const { scope, client, catalog } = setup()
    let finish!: (record: SessionRecord) => void
    client.generateSessionTitle.mockImplementation(
      () =>
        new Promise((resolve) => {
          finish = resolve
        }),
    )
    try {
      catalog.accepted(first, true)
      catalog.messageAvailable(first)
      await flushPromises()
      expect(client.generateSessionTitle).toHaveBeenCalledOnce()
      await catalog.rename(first, 'Manual')
      finish({ ...first, title: 'Stale generated title', titleVersion: 2 })
      await catalog.load()
      await flushPromises()
      expect(catalog.sessions.value.find((item) => item.agentId === 'z')?.title).toBe(
        'Manual',
      )
      catalog.messageAvailable(first)
      expect(client.generateSessionTitle).toHaveBeenCalledOnce()
    } finally {
      scope.stop()
    }
  })

  it('does not name historical or manually renamed conversations and does not retry model failures', async () => {
    const { scope, client, catalog } = setup()
    try {
      await catalog.load()
      catalog.messageAvailable(first)
      catalog.accepted(first, true)
      await catalog.rename(first, 'Manual')
      catalog.messageAvailable(first)
      await flushPromises()
      expect(client.generateSessionTitle).not.toHaveBeenCalled()
      client.generateSessionTitle.mockRejectedValue(new Error('model unavailable'))
      catalog.accepted(second, true)
      catalog.messageAvailable(second)
      await flushPromises()
      catalog.accepted(second, false)
      catalog.messageAvailable(second)
      await flushPromises()
      expect(client.generateSessionTitle).toHaveBeenCalledTimes(1)
      expect(catalog.sessions.value.find((item) => item.agentId === 'a')?.title).toBe(
        'Second',
      )
    } finally {
      scope.stop()
    }
  })

  it('reloads the winning title after a generation conflict without retrying the model', async () => {
    const { scope, client, catalog } = setup()
    client.generateSessionTitle.mockImplementation(async () => {
      client.sessions.mockResolvedValue({
        sessions: [{ ...first, title: 'Other tab', titleVersion: 2 }],
        nextCursor: null,
      })
      throw new RuntimeHttpError(409, 'session_title_conflict', 'conflict')
    })
    try {
      catalog.accepted(first, true)
      catalog.messageAvailable(first)
      await flushPromises()
      expect(catalog.sessions.value[0]?.title).toBe('Other tab')
      expect(client.generateSessionTitle).toHaveBeenCalledTimes(1)
    } finally {
      scope.stop()
    }
  })
})
