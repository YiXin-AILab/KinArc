import { afterEach, describe, expect, it, vi } from 'vitest'

import { RuntimeHttpError, createRuntimeClient } from './client'

const scope = {
  sessionId: 'session/with spaces',
  agentId: 'test-agent-stream',
}

/** 使用测试拥有的传输实现创建未鉴权客户端。 */
function clientFor(fetchMock: typeof fetch) {
  return createRuntimeClient(
    { apiBaseUrl: '', appId: 'web', userId: 'user' },
    fetchMock,
  )
}

describe('runtime client', () => {
  it('reads the ordered Todo list with the full session identity', async () => {
    const todos = [{ content: '确认需求', status: 'pending' }]
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValue(new Response(JSON.stringify({ todos })))
    const controller = new AbortController()
    await expect(clientFor(fetchMock).todos(scope, controller.signal)).resolves.toEqual(
      todos,
    )
    expect(fetchMock.mock.calls[0]![0]).toBe(
      '/v1/sessions/session%2Fwith%20spaces/todos?app_id=web&user_id=user&agent_id=test-agent-stream',
    )
    controller.abort()
    expect(fetchMock.mock.calls[0]![1]?.signal?.aborted).toBe(true)
  })

  afterEach(() => vi.useRealTimers())

  it('soft-deletes the scoped session and accepts an empty 204 response', async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValue(new Response(null, { status: 204 }))
    await expect(clientFor(fetchMock).deleteSession(scope)).resolves.toBeUndefined()
    const [url, init] = fetchMock.mock.calls[0]!
    expect(url).toBe('/v1/sessions/session%2Fwith%20spaces/delete')
    expect(init?.method).toBe('POST')
    expect(JSON.parse(String(init?.body))).toEqual({
      app_id: 'web',
      user_id: 'user',
      agent_id: scope.agentId,
      hard_delete: false,
    })
  })

  it('maps paginated sessions and both title commands using the full public scope', async () => {
    const record = {
      app_id: 'web',
      user_id: 'user',
      agent_id: scope.agentId,
      session_id: scope.sessionId,
      title: 'Server title',
      created_at: '2026-09-24T00:00:00Z',
      updated_at: '2026-09-24T00:00:00Z',
      title_version: 2,
    }
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockImplementation(
        async (_url, init) =>
          new Response(
            JSON.stringify(
              init?.method === 'POST'
                ? record
                : { sessions: [record], next_cursor: 'next' },
            ),
          ),
      )
    const client = clientFor(fetchMock)
    const page = await client.sessions('older cursor')
    expect(page).toEqual({
      sessions: [
        {
          sessionId: scope.sessionId,
          agentId: scope.agentId,
          title: 'Server title',
          createdAt: record.created_at,
          updatedAt: record.updated_at,
          titleVersion: 2,
        },
      ],
      nextCursor: 'next',
    })
    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      '/v1/sessions?app_id=web&user_id=user&limit=20&cursor=older+cursor',
    )
    await expect(client.renameSession(scope, 'New title')).resolves.toEqual(
      page.sessions[0],
    )
    await expect(client.generateSessionTitle(scope)).resolves.toEqual(page.sessions[0])
    expect(
      fetchMock.mock.calls
        .slice(1)
        .map(([url, init]) => [url, JSON.parse(String(init?.body))]),
    ).toEqual([
      [
        '/v1/sessions/session%2Fwith%20spaces',
        {
          app_id: 'web',
          user_id: 'user',
          agent_id: 'test-agent-stream',
          title: 'New title',
        },
      ],
      [
        '/v1/sessions/session%2Fwith%20spaces/title/generate',
        { app_id: 'web', user_id: 'user', agent_id: 'test-agent-stream' },
      ],
    ])
  })

  it('does not start a request after its caller has cancelled', async () => {
    vi.useFakeTimers()
    const fetchMock = vi.fn<typeof fetch>()
    const client = clientFor(fetchMock)
    const controller = new AbortController()
    controller.abort()

    await expect(client.agents(controller.signal)).rejects.toBe(
      controller.signal.reason,
    )
    expect(fetchMock).not.toHaveBeenCalled()
    expect(vi.getTimerCount()).toBe(0)
  })

  it.each(['headers', 'json', 'error'] as const)(
    'bounds ordinary requests while waiting for %s',
    async (phase) => {
      vi.useFakeTimers()
      const fetchMock = vi.fn<typeof fetch>().mockImplementation((_url, init) => {
        const signal = init?.signal
        const stalled = new Promise<never>((_resolve, reject) => {
          signal?.addEventListener('abort', () => reject(signal.reason), { once: true })
        })
        if (phase === 'headers') return stalled
        const response = new Response('', { status: phase === 'error' ? 500 : 200 })
        vi.spyOn(response, 'json').mockReturnValue(stalled)
        return Promise.resolve(response)
      })
      const client = clientFor(fetchMock)
      const result = client.agents().catch((error: unknown) => error)
      await vi.advanceTimersByTimeAsync(30_000)
      expect(fetchMock.mock.calls[0]?.[1]?.signal?.aborted).toBe(true)
      await expect(result).resolves.toMatchObject({ name: 'TimeoutError' })
      expect(vi.getTimerCount()).toBe(0)
    },
  )

  it.each(['upload', 'download'] as const)(
    'allows longer bounded %s transfers',
    async (kind) => {
      vi.useFakeTimers()
      const fetchMock = vi.fn<typeof fetch>().mockImplementation((_url, init) => {
        const signal = init?.signal
        const stalled = new Promise<never>((_resolve, reject) => {
          signal?.addEventListener('abort', () => reject(signal.reason), { once: true })
        })
        const response = new Response('')
        vi.spyOn(response, kind === 'upload' ? 'json' : 'blob').mockReturnValue(stalled)
        return Promise.resolve(response)
      })
      const client = clientFor(fetchMock)
      const result = (
        kind === 'upload'
          ? client.uploadAsset(new File(['asset'], 'asset.txt'))
          : client.readAsset('asset')
      ).catch((error: unknown) => error)
      await vi.advanceTimersByTimeAsync(30_000)
      expect(fetchMock.mock.calls[0]?.[1]?.signal?.aborted).toBe(false)
      await vi.advanceTimersByTimeAsync(90_000)
      expect(fetchMock.mock.calls[0]?.[1]?.signal?.aborted).toBe(true)
      await expect(result).resolves.toMatchObject({ name: 'TimeoutError' })
    },
  )

  it('honors caller cancellation and clears the deadline', async () => {
    vi.useFakeTimers()
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(
      (_url, init) =>
        new Promise((_resolve, reject) => {
          init?.signal?.addEventListener('abort', () => reject(init.signal?.reason), {
            once: true,
          })
        }),
    )
    const client = clientFor(fetchMock)
    const controller = new AbortController()
    const result = client
      .cancel(scope, controller.signal)
      .catch((error: unknown) => error)
    controller.abort()
    await expect(result).resolves.toBe(controller.signal.reason)
    expect(vi.getTimerCount()).toBe(0)
  })

  it('bounds SSE connection establishment', async () => {
    vi.useFakeTimers()
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(
      (_url, init) =>
        new Promise((_resolve, reject) => {
          init?.signal?.addEventListener('abort', () => reject(init.signal?.reason), {
            once: true,
          })
        }),
    )
    const client = clientFor(fetchMock)
    const result = client
      .events(scope, undefined, new AbortController().signal)
      .catch((error: unknown) => error)
    await vi.advanceTimersByTimeAsync(30_000)
    expect(fetchMock.mock.calls[0]?.[1]?.signal?.aborted).toBe(true)
    await expect(result).resolves.toMatchObject({ name: 'TimeoutError' })
  })

  it('loads and maps the authorized Agent catalog', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(
        JSON.stringify({
          agents: [
            {
              id: 'writer',
              name: 'Writer',
              description: 'Writes',
              output_mode: 'complete',
            },
          ],
        }),
        { headers: { 'content-type': 'application/json' } },
      ),
    )
    const client = createRuntimeClient(
      {
        apiBaseUrl: 'https://api.example.com',
        appId: 'web',
        userId: 'user',
        tokenProvider: () => 'runtime-token',
      },
      fetchMock,
    )

    await expect(client.agents()).resolves.toEqual([
      {
        id: 'writer',
        name: 'Writer',
        description: 'Writes',
        outputMode: 'complete',
      },
    ])
    const [url, init] = fetchMock.mock.calls[0] ?? []
    expect(url).toBe('https://api.example.com/v1/agents')
    expect(new Headers(init?.headers).get('Authorization')).toBe('Bearer runtime-token')
  })

  it('rejects a malformed Agent catalog instead of treating it as empty', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(
        JSON.stringify({
          agents: [
            {
              id: 'writer',
              name: '',
              description: 'Writes',
              output_mode: 'complete',
            },
          ],
        }),
        { headers: { 'content-type': 'application/json' } },
      ),
    )
    const client = clientFor(fetchMock)

    await expect(client.agents()).rejects.toThrow(Error)
  })

  it('submits queue and steer messages with the complete public scope', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ session_id: 'session', message_id: 'message-1' }), {
        status: 202,
        headers: { 'content-type': 'application/json' },
      }),
    )
    const client = createRuntimeClient(
      { apiBaseUrl: 'https://api.example.com', appId: 'web', userId: 'user' },
      fetchMock,
    )

    await client.submitMessage(scope, {
      messageId: 'message-1',
      role: 'user',
      origin: 'human.agent',
      content: [
        {
          type: 'image',
          source: {
            type: 'asset',
            asset_id: 'asset-1',
            media_type: 'image/png',
            filename: 'image.png',
          },
          detail: 'auto',
        },
        { type: 'text', text: '你好' },
      ],
      mode: 'queue',
    })
    await client.submitMessage(scope, {
      messageId: 'message-2',
      role: 'system',
      origin: 'order.workflow',
      content: [{ type: 'text', text: 'refunded' }],
      mode: 'steer',
    })

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      'https://api.example.com/v1/sessions/session%2Fwith%20spaces/messages',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          app_id: 'web',
          user_id: 'user',
          agent_id: 'test-agent-stream',
          message_id: 'message-1',
          role: 'user',
          origin: 'human.agent',
          content: [
            {
              type: 'image',
              source: {
                type: 'asset',
                asset_id: 'asset-1',
                media_type: 'image/png',
                filename: 'image.png',
              },
              detail: 'auto',
            },
            { type: 'text', text: '你好' },
          ],
          mode: 'queue',
        }),
      }),
    )
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      'https://api.example.com/v1/sessions/session%2Fwith%20spaces/messages',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          app_id: 'web',
          user_id: 'user',
          agent_id: 'test-agent-stream',
          message_id: 'message-2',
          role: 'system',
          origin: 'order.workflow',
          content: [{ type: 'text', text: 'refunded' }],
          mode: 'steer',
        }),
      }),
    )
  })

  it('cancels a session without deprecated reason or internal run fields', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(
        JSON.stringify({
          session_id: 'session/with spaces',
          status: 'accepted',
        }),
        {
          status: 202,
          headers: { 'content-type': 'application/json' },
        },
      ),
    )
    const client = createRuntimeClient(
      {
        apiBaseUrl: 'https://api.example.com',
        appId: 'web',
        userId: 'user',
      },
      fetchMock,
    )

    await client.cancel(scope)

    expect(fetchMock).toHaveBeenCalledWith(
      'https://api.example.com/v1/sessions/session%2Fwith%20spaces/cancel',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          app_id: 'web',
          user_id: 'user',
          agent_id: 'test-agent-stream',
        }),
      }),
    )
  })

  it('loads minimal pending inputs without delivery status', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(
        JSON.stringify({
          session_id: 'session/with spaces',
          inputs: [
            {
              message_id: 'pending-1',
              role: 'system',
              origin: 'workflow.pending',
              content: '继续检查',
              blocks: [
                {
                  type: 'image',
                  source_type: 'asset',
                  media_type: 'image/png',
                  filename: 'image.png',
                  asset_id: 'asset-1',
                },
                { type: 'text', text: '继续检查' },
              ],
              mode: 'steer',
              created_at: '2026-08-05T10:00:00Z',
            },
          ],
        }),
        { headers: { 'content-type': 'application/json' } },
      ),
    )
    const client = clientFor(fetchMock)

    const pending = await client.pendingInputs(scope)

    expect(pending).toEqual([
      {
        messageId: 'pending-1',
        role: 'system',
        origin: 'workflow.pending',
        content: '继续检查',
        blocks: [
          {
            type: 'image',
            source_type: 'asset',
            media_type: 'image/png',
            filename: 'image.png',
            asset_id: 'asset-1',
          },
          { type: 'text', text: '继续检查' },
        ],
        mode: 'steer',
        createdAt: '2026-08-05T10:00:00Z',
      },
    ])
    expect(fetchMock).toHaveBeenCalledWith(
      '/v1/sessions/session%2Fwith%20spaces/inputs/pending?app_id=web&user_id=user&agent_id=test-agent-stream',
      expect.anything(),
    )
    expect(JSON.stringify(pending)).not.toContain('status')
  })

  it('uploads a file as multipart and returns its AssetSource', async () => {
    const upload = {
      asset_id: 'asset-1',
      media_type: 'image/png',
      filename: 'shot.png',
      size_bytes: 3,
      checksum_sha256: 'a'.repeat(64),
      source: {
        type: 'asset',
        asset_id: 'asset-1',
        media_type: 'image/png',
        filename: 'shot.png',
      },
    }
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify(upload), {
        status: 201,
        headers: { 'content-type': 'application/json' },
      }),
    )
    const client = createRuntimeClient(
      {
        apiBaseUrl: 'https://api.example.com',
        appId: 'web',
        userId: 'user',
        tokenProvider: () => 'runtime-token',
      },
      fetchMock,
    )
    const file = new File(['png'], 'shot.png', { type: 'image/png' })

    const result = await client.uploadAsset(file)

    const [url, init] = fetchMock.mock.calls[0] ?? []
    expect(url).toBe('https://api.example.com/v1/assets')
    expect(init?.method).toBe('POST')
    expect(init?.body).toBeInstanceOf(FormData)
    expect((init?.body as FormData).get('file')).toBe(file)
    expect(new Headers(init?.headers).get('Content-Type')).toBeNull()
    expect(new Headers(init?.headers).get('Authorization')).toBe('Bearer runtime-token')
    expect(result.source.asset_id).toBe('asset-1')
  })

  it('reads a path asset as Blob with authorization', async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValue(
        new Response('asset', { headers: { 'content-type': 'image/png' } }),
      )
    const client = createRuntimeClient(
      {
        apiBaseUrl: 'https://api.example.com',
        appId: 'web',
        userId: 'user',
        tokenProvider: () => 'runtime-token',
      },
      fetchMock,
    )

    const result = await client.readAsset('group 2/M00/a.png')

    const [url, init] = fetchMock.mock.calls[0] ?? []
    expect(url).toBe('https://api.example.com/v1/assets/group%202/M00/a.png')
    expect(new Headers(init?.headers).get('Authorization')).toBe('Bearer runtime-token')
    expect(await result.text()).toBe('asset')
  })

  it('opens fetch SSE with scope, authorization, cursor and abort signal', async () => {
    vi.useFakeTimers()
    const stream = new ReadableStream<Uint8Array>()
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(stream, {
        headers: { 'content-type': 'text/event-stream; charset=utf-8' },
      }),
    )
    const client = createRuntimeClient(
      {
        apiBaseUrl: '',
        appId: 'web',
        userId: 'user',
        tokenProvider: () => 'secret-at-runtime',
      },
      fetchMock,
    )
    const controller = new AbortController()

    const response = await client.events(scope, 'epoch:1-0', controller.signal)

    expect(response.body).toBe(stream)
    const [url, init] = fetchMock.mock.calls[0] ?? []
    expect(url).toBe(
      '/v1/sessions/session%2Fwith%20spaces/events?app_id=web&user_id=user&agent_id=test-agent-stream',
    )
    expect(new Headers(init?.headers).get('Last-Event-ID')).toBe('epoch:1-0')
    expect(new Headers(init?.headers).get('Authorization')).toBe(
      'Bearer secret-at-runtime',
    )
    expect(vi.getTimerCount()).toBe(0)
    await vi.advanceTimersByTimeAsync(120_000)
    expect(init?.signal?.aborted).toBe(false)
    controller.abort()
    expect(init?.signal?.aborted).toBe(true)
  })

  it('leaves retryAfterSeconds undefined when the Retry-After header is absent', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ code: 'conflict' }), {
        status: 409,
        headers: { 'content-type': 'application/json' },
      }),
    )
    const client = clientFor(fetchMock)

    const failure = await client.agents().catch((e: unknown) => e)

    expect(failure).toBeInstanceOf(RuntimeHttpError)
    expect((failure as RuntimeHttpError).retryAfterSeconds).toBeUndefined()
  })

  it.each([
    ['5', 5],
    ['1.5', 1.5],
  ])(
    'maps a valid Retry-After header %s to retryAfterSeconds',
    async (header, expected) => {
      const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
        new Response(JSON.stringify({ code: 'throttled' }), {
          status: 429,
          headers: { 'content-type': 'application/json', 'retry-after': header },
        }),
      )
      const client = clientFor(fetchMock)

      const failure = await client.agents().catch((e: unknown) => e)

      expect((failure as RuntimeHttpError).retryAfterSeconds).toBe(expected)
    },
  )

  it.each(['-3', 'abc', 'Infinity'])(
    'leaves retryAfterSeconds undefined for invalid Retry-After header %s',
    async (header) => {
      const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
        new Response(JSON.stringify({ code: 'throttled' }), {
          status: 429,
          headers: { 'content-type': 'application/json', 'retry-after': header },
        }),
      )
      const client = clientFor(fetchMock)

      const failure = await client.agents().catch((e: unknown) => e)

      expect(failure).toBeInstanceOf(RuntimeHttpError)
      expect((failure as RuntimeHttpError).retryAfterSeconds).toBeUndefined()
    },
  )

  it('maps a stable server error body to RuntimeHttpError', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ code: 'cursor_expired' }), {
        status: 410,
        headers: { 'content-type': 'application/json' },
      }),
    )
    const client = clientFor(fetchMock)

    await expect(
      client.events(scope, 'old', new AbortController().signal),
    ).rejects.toEqual(
      expect.objectContaining<Partial<RuntimeHttpError>>({
        status: 410,
        code: 'cursor_expired',
      }),
    )
  })
})
