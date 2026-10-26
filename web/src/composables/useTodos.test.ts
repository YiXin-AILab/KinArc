import { flushPromises } from '@vue/test-utils'
import { effectScope } from 'vue'
import { describe, expect, it, vi } from 'vitest'
import type { TodoItem } from '../domain/types'
import { useTodos } from './useTodos'

const first = { sessionId: 'one', agentId: 'agent' }
const items: TodoItem[] = [{ content: '确认需求', status: 'in_progress' }]

describe('useTodos', () => {
  it('coalesces refreshes and reads again after an in-flight response', async () => {
    let resolve!: (value: TodoItem[]) => void
    const read = vi
      .fn()
      .mockImplementationOnce(
        () =>
          new Promise<TodoItem[]>((done) => {
            resolve = done
          }),
      )
      .mockResolvedValue([])
    const scope = effectScope()
    const state = scope.run(() => useTodos({ todos: read }))!
    state.reset(first)
    const pending = state.refresh()
    void state.refresh()
    void state.refresh()
    expect(read).toHaveBeenCalledTimes(1)
    resolve(items)
    await pending
    expect(read).toHaveBeenCalledTimes(2)
    expect(state.items.value).toEqual([])
    scope.stop()
  })

  it('discards old session responses even when transport ignores cancellation', async () => {
    let resolve!: (value: TodoItem[]) => void
    const read = vi
      .fn()
      .mockImplementationOnce(
        () =>
          new Promise<TodoItem[]>((done) => {
            resolve = done
          }),
      )
      .mockResolvedValue([])
    const scope = effectScope()
    const state = scope.run(() => useTodos({ todos: read }))!
    state.reset(first)
    const pending = state.refresh()
    const signal = read.mock.calls[0]![1] as AbortSignal
    state.reset({ ...first, agentId: 'another' })
    await state.refresh()
    resolve(items)
    await pending
    expect(signal.aborted).toBe(true)
    expect(state.items.value).toEqual([])
    scope.stop()
  })

  it('retains data on reconnect failures, supports retry and cancels on disposal', async () => {
    const read = vi
      .fn()
      .mockResolvedValueOnce(items)
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValue([])
    const scope = effectScope()
    const state = scope.run(() => useTodos({ todos: read }))!
    state.reset(first)
    await state.refresh()
    state.reset(first)
    await state.refresh()
    expect(state.items.value).toEqual(items)
    expect(state.error.value).toBe(true)
    await state.refresh()
    expect(state.items.value).toEqual([])
    expect(state.error.value).toBe(false)
    scope.stop()
    await state.refresh()
    await flushPromises()
    expect(read).toHaveBeenCalledTimes(3)
  })
})
