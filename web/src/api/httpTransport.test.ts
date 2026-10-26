import { afterEach, describe, expect, it, vi } from 'vitest'

import { RuntimeHttpError } from './client'
import { createHttpTransport } from './httpTransport'

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

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('createHttpTransport requestJson', () => {
  afterEach(() => vi.useRealTimers())

  it('sends JSON body, authorization and resolves parsed JSON', async () => {
    const fetchImpl = vi
      .fn<typeof fetch>()
      .mockResolvedValue(jsonResponse({ ok: true }))
    const transport = createHttpTransport({ tokenProvider: () => ' token ' }, fetchImpl)

    const result = await transport.requestJson<{ ok: boolean }>('/v1/x', {
      method: 'POST',
      body: { a: 1 },
    })

    const [url, init] = fetchImpl.mock.calls[0]!
    expect(url).toBe('/v1/x')
    expect(init?.method).toBe('POST')
    expect(new Headers(init?.headers).get('Authorization')).toBe('Bearer token')
    expect(new Headers(init?.headers).get('Content-Type')).toBe('application/json')
    expect(JSON.parse(String(init?.body))).toEqual({ a: 1 })
    expect(result).toEqual({ ok: true })
  })

  it('omits the authorization header when the token is absent or blank', async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse({}))
    const transport = createHttpTransport({ tokenProvider: () => '  ' }, fetchImpl)

    await transport.requestJson('/v1/x')

    expect(
      new Headers(fetchImpl.mock.calls[0]?.[1]?.headers).get('Authorization'),
    ).toBeNull()
  })

  it('sends multipart form data without a manual Content-Type', async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse({}))
    const transport = createHttpTransport({}, fetchImpl)
    const formData = new FormData()
    formData.append('file', new File(['x'], 'a.txt'))

    await transport.requestJson('/v1/upload', { method: 'POST', formData })

    const init = fetchImpl.mock.calls[0]?.[1]
    expect(init?.body).toBe(formData)
    expect(new Headers(init?.headers).get('Content-Type')).toBeNull()
  })

  it('tolerates an empty success body as undefined', async () => {
    const fetchImpl = vi
      .fn<typeof fetch>()
      .mockResolvedValue(new Response(null, { status: 201 }))
    const transport = createHttpTransport({}, fetchImpl)

    await expect(transport.requestJson('/v1/x')).resolves.toBeUndefined()
  })

  it('aborts a stalled request at the default deadline', async () => {
    vi.useFakeTimers()
    const fetchImpl = stalledFetch()
    const transport = createHttpTransport({}, fetchImpl)

    const result = transport.requestJson('/v1/x').catch((error: unknown) => error)
    await vi.advanceTimersByTimeAsync(30_000)

    expect(fetchImpl.mock.calls[0]?.[1]?.signal?.aborted).toBe(true)
    await expect(result).resolves.toMatchObject({ name: 'TimeoutError' })
    expect(vi.getTimerCount()).toBe(0)
  })

  it('bounds body consumption within the deadline', async () => {
    vi.useFakeTimers()
    const fetchImpl = vi.fn<typeof fetch>().mockImplementation((_url, init) => {
      const stalled = new Promise<never>((_resolve, reject) => {
        init?.signal?.addEventListener('abort', () => reject(init.signal?.reason), {
          once: true,
        })
      })
      const response = new Response('', { status: 200 })
      vi.spyOn(response, 'text').mockReturnValue(stalled)
      return Promise.resolve(response)
    })
    const transport = createHttpTransport({}, fetchImpl)

    const result = transport.requestJson('/v1/x').catch((error: unknown) => error)
    await vi.advanceTimersByTimeAsync(30_000)

    await expect(result).resolves.toMatchObject({ name: 'TimeoutError' })
    expect(vi.getTimerCount()).toBe(0)
  })

  it('does not start a request whose caller signal is already aborted', async () => {
    vi.useFakeTimers()
    const fetchImpl = stalledFetch()
    const transport = createHttpTransport({}, fetchImpl)
    const controller = new AbortController()
    controller.abort()

    await expect(
      transport.requestJson('/v1/x', { signal: controller.signal }),
    ).rejects.toBe(controller.signal.reason)
    expect(fetchImpl).not.toHaveBeenCalled()
    expect(vi.getTimerCount()).toBe(0)
  })

  it('propagates caller cancellation and clears the deadline', async () => {
    vi.useFakeTimers()
    const fetchImpl = stalledFetch()
    const transport = createHttpTransport({}, fetchImpl)
    const controller = new AbortController()

    const result = transport
      .requestJson('/v1/x', { signal: controller.signal })
      .catch((error: unknown) => error)
    controller.abort()

    await expect(result).resolves.toBe(controller.signal.reason)
    expect(vi.getTimerCount()).toBe(0)
  })

  it('maps backend error bodies to RuntimeHttpError', async () => {
    const fetchImpl = vi
      .fn<typeof fetch>()
      .mockResolvedValue(
        jsonResponse({ code: 'kb_not_found', message: '知识库不存在' }, 404),
      )
    const transport = createHttpTransport({}, fetchImpl)

    const failure = await transport.requestJson('/v1/x').catch((e: unknown) => e)

    expect(failure).toBeInstanceOf(RuntimeHttpError)
    expect(failure).toMatchObject({ status: 404, code: 'kb_not_found' })
  })

  it('maps FastAPI-style detail bodies to the error message', async () => {
    const fetchImpl = vi
      .fn<typeof fetch>()
      .mockResolvedValue(jsonResponse({ detail: '不支持的文件类型' }, 415))
    const transport = createHttpTransport({}, fetchImpl)

    const failure = await transport.requestJson('/v1/x').catch((e: unknown) => e)

    expect(failure).toBeInstanceOf(RuntimeHttpError)
    expect((failure as RuntimeHttpError).message).toBe('不支持的文件类型')
  })

  it('leaves retryAfterSeconds undefined when the Retry-After header is absent', async () => {
    const fetchImpl = vi
      .fn<typeof fetch>()
      .mockResolvedValue(jsonResponse({ code: 'conflict' }, 409))
    const transport = createHttpTransport({}, fetchImpl)

    const failure = await transport.requestJson('/v1/x').catch((e: unknown) => e)

    expect(failure).toBeInstanceOf(RuntimeHttpError)
    expect((failure as RuntimeHttpError).retryAfterSeconds).toBeUndefined()
  })

  it.each([
    ['5', 5],
    ['1.5', 1.5],
  ])(
    'maps a valid Retry-After header %s to retryAfterSeconds',
    async (header, expected) => {
      const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(
        new Response(JSON.stringify({ code: 'throttled' }), {
          status: 429,
          headers: { 'content-type': 'application/json', 'retry-after': header },
        }),
      )
      const transport = createHttpTransport({}, fetchImpl)

      const failure = await transport.requestJson('/v1/x').catch((e: unknown) => e)

      expect((failure as RuntimeHttpError).retryAfterSeconds).toBe(expected)
    },
  )

  it.each(['-3', 'abc', 'Infinity'])(
    'leaves retryAfterSeconds undefined for invalid Retry-After header %s',
    async (header) => {
      const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(
        new Response(JSON.stringify({ code: 'throttled' }), {
          status: 429,
          headers: { 'content-type': 'application/json', 'retry-after': header },
        }),
      )
      const transport = createHttpTransport({}, fetchImpl)

      const failure = await transport.requestJson('/v1/x').catch((e: unknown) => e)

      expect(failure).toBeInstanceOf(RuntimeHttpError)
      expect((failure as RuntimeHttpError).retryAfterSeconds).toBeUndefined()
    },
  )

  it('falls back to a generic error for non-JSON error bodies', async () => {
    const fetchImpl = vi
      .fn<typeof fetch>()
      .mockResolvedValue(new Response('oops', { status: 502 }))
    const transport = createHttpTransport({}, fetchImpl)

    const failure = await transport.requestJson('/v1/x').catch((e: unknown) => e)

    expect(failure).toMatchObject({ status: 502, code: 'http_error' })
  })
})

describe('createHttpTransport openStream', () => {
  afterEach(() => vi.useRealTimers())

  it('bounds connection establishment with a deadline', async () => {
    vi.useFakeTimers()
    const fetchImpl = stalledFetch()
    const transport = createHttpTransport({}, fetchImpl)

    const result = transport.openStream('/v1/events').catch((error: unknown) => error)
    await vi.advanceTimersByTimeAsync(30_000)

    expect(fetchImpl.mock.calls[0]?.[1]?.signal?.aborted).toBe(true)
    await expect(result).resolves.toMatchObject({ name: 'TimeoutError' })
    expect(vi.getTimerCount()).toBe(0)
  })

  it('applies no total deadline to an established stream but keeps caller cancellation', async () => {
    vi.useFakeTimers()
    const stream = new ReadableStream<Uint8Array>()
    const fetchImpl = vi
      .fn<typeof fetch>()
      .mockResolvedValue(
        new Response(stream, { headers: { 'content-type': 'text/event-stream' } }),
      )
    const transport = createHttpTransport({}, fetchImpl)
    const controller = new AbortController()

    const response = await transport.openStream('/v1/events', {
      signal: controller.signal,
    })

    expect(response.body).toBe(stream)
    const signal = fetchImpl.mock.calls[0]?.[1]?.signal
    expect(vi.getTimerCount()).toBe(0)
    await vi.advanceTimersByTimeAsync(120_000)
    expect(signal?.aborted).toBe(false)
    controller.abort()
    expect(signal?.aborted).toBe(true)
  })

  it('maps connection failures to RuntimeHttpError', async () => {
    const fetchImpl = vi
      .fn<typeof fetch>()
      .mockResolvedValue(jsonResponse({ code: 'run_not_found' }, 404))
    const transport = createHttpTransport({}, fetchImpl)

    await expect(transport.openStream('/v1/events')).rejects.toMatchObject({
      status: 404,
      code: 'run_not_found',
    })
  })
})
