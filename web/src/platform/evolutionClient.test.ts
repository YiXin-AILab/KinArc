import { afterEach, describe, expect, it, vi } from 'vitest'

import { createEvolutionClient, type EvolutionProgressEvent } from './evolutionClient'

/** 模拟持续挂起的 fetch，仅在请求信号取消时拒绝 Promise。 */
function stalledFetch() {
  return vi.fn<typeof fetch>().mockImplementation(
    (_url, init) =>
      new Promise<never>((_resolve, reject) => {
        init?.signal?.addEventListener('abort', () => reject(init.signal?.reason), {
          once: true,
        })
      }),
  )
}

describe('createEvolutionClient openRunEvents', () => {
  afterEach(() => vi.useRealTimers())

  it('bounds connection establishment and reports failure after retries run out', async () => {
    vi.useFakeTimers()
    const fetchImpl = stalledFetch()
    const client = createEvolutionClient({ apiBaseUrl: '' }, fetchImpl)
    const controller = new AbortController()
    const onError = vi.fn()

    client.openRunEvents('run-1', {
      signal: controller.signal,
      onEvent: vi.fn(),
      onError,
    })
    // 覆盖七次各 30 秒的连接超时，以及有上限的退避等待。
    await vi.advanceTimersByTimeAsync(300_000)

    expect(onError).toHaveBeenCalledTimes(1)
    expect(onError.mock.calls[0]?.[0]?.message).toContain('TimeoutError')
    // 首次连接加 MAX_RECONNECT_ATTEMPTS 次重连，每次均触发 30 秒超时。
    expect(fetchImpl).toHaveBeenCalledTimes(7)
    expect(vi.getTimerCount()).toBe(0)
  })

  it('stops retrying silently when the caller cancels during connection backoff', async () => {
    vi.useFakeTimers()
    const fetchImpl = stalledFetch()
    const client = createEvolutionClient({ apiBaseUrl: '' }, fetchImpl)
    const controller = new AbortController()
    const onError = vi.fn()

    client.openRunEvents('run-1', {
      signal: controller.signal,
      onEvent: vi.fn(),
      onError,
    })
    // 首次连接触发 30 秒超时，随后进入退避等待。
    await vi.advanceTimersByTimeAsync(30_000)
    controller.abort()
    await vi.advanceTimersByTimeAsync(300_000)

    expect(onError).not.toHaveBeenCalled()
    expect(fetchImpl).toHaveBeenCalledTimes(1)
    expect(vi.getTimerCount()).toBe(0)
  })

  it('keeps an established stream free of any total deadline', async () => {
    vi.useFakeTimers()
    let streamController!: ReadableStreamDefaultController<Uint8Array>
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        streamController = controller
      },
    })
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(stream, {
        headers: { 'content-type': 'text/event-stream' },
      }),
    )
    const client = createEvolutionClient({ apiBaseUrl: '' }, fetchImpl)
    const controller = new AbortController()
    const events: EvolutionProgressEvent[] = []

    client.openRunEvents('run-1', {
      signal: controller.signal,
      onEvent: (event) => events.push(event),
    })
    await vi.advanceTimersByTimeAsync(0)
    // 超过普通请求的超时后，已建立的流仍能接收事件。
    await vi.advanceTimersByTimeAsync(120_000)

    const frame = {
      cursor: 7,
      ts: 1,
      event: 'log',
      message: 'hello',
      fields: {},
    }
    streamController.enqueue(
      new TextEncoder().encode(`id: 7\ndata: ${JSON.stringify(frame)}\n\n`),
    )
    await vi.advanceTimersByTimeAsync(0)

    expect(events).toEqual([frame])
    expect(fetchImpl.mock.calls[0]?.[1]?.signal?.aborted).toBe(false)
  })
})
