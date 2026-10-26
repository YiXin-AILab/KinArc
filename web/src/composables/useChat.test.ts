import { flushPromises } from '@vue/test-utils'
import { effectScope } from 'vue'
import { describe, expect, it, vi } from 'vitest'

import type { RuntimeClient } from '../api/client'
import { RuntimeHttpError } from '../api/client'
import type { AgentDefinition } from '../config'
import type {
  InputContentBlock,
  PublicEventEnvelope,
  SessionRecord,
  SessionSnapshot,
} from '../domain/types'
import { createCheckpointStore } from '../storage/sessionStore'
import { type ChatRuntimeConfig, type UseChatOptions, useChat } from './useChat'

const streamAgent: AgentDefinition = {
  id: 'stream-agent',
  name: 'Stream',
  description: '',
  outputMode: 'stream',
}

function memoryStorage(): Storage {
  const values = new Map<string, string>()
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

function config(agent: AgentDefinition): ChatRuntimeConfig {
  return { apiBaseUrl: '', appId: 'web', userId: 'user', agents: [agent] }
}

function textBlocks(text: string): InputContentBlock[] {
  return [{ type: 'text', text }]
}

function imageBlocks(text = ''): InputContentBlock[] {
  const blocks: InputContentBlock[] = [
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
  ]
  if (text) blocks.push({ type: 'text', text })
  return blocks
}

function client(overrides: Partial<RuntimeClient>): RuntimeClient {
  return {
    agents: vi.fn().mockResolvedValue([]),
    sessions: vi.fn().mockResolvedValue({ sessions: [], nextCursor: null }),
    renameSession: vi.fn(),
    generateSessionTitle: vi.fn(),
    deleteSession: vi.fn().mockResolvedValue(undefined),
    uploadAsset: vi.fn(),
    readAsset: vi.fn(),
    submitMessage: vi.fn(),
    resume: vi.fn(),
    cancel: vi.fn(),
    snapshot: vi.fn().mockResolvedValue({
      session_id: 'session',
      status: 'idle',
    }),
    messages: vi.fn().mockResolvedValue([]),
    pendingInputs: vi.fn().mockResolvedValue([]),
    todos: vi.fn().mockResolvedValue([]),
    events: vi.fn(),
    ...overrides,
  }
}

function streamResponse(frame: string): Response {
  return new Response(
    new ReadableStream({
      start(controller) {
        controller.enqueue(new TextEncoder().encode(frame))
        controller.close()
      },
    }),
    { headers: { 'content-type': 'text/event-stream' } },
  )
}

function controlledStreamResponse(
  firstFrame: string,
  finalFrame: string,
): { response: Response; finish: () => void } {
  let finish = (): void => {
    throw new Error('stream is not ready')
  }
  const response = new Response(
    new ReadableStream({
      start(controller) {
        controller.enqueue(new TextEncoder().encode(firstFrame))
        finish = () => {
          controller.enqueue(new TextEncoder().encode(finalFrame))
          controller.close()
        }
      },
    }),
    { headers: { 'content-type': 'text/event-stream' } },
  )
  return { response, finish: () => finish() }
}

function pushableStreamResponse(): {
  response: Response
  push: (frame: string) => void
  close: () => void
} {
  let push = (_frame: string): void => {
    throw new Error('stream is not ready')
  }
  let close = (): void => {
    throw new Error('stream is not ready')
  }
  const response = new Response(
    new ReadableStream({
      start(controller) {
        push = (frame) => controller.enqueue(new TextEncoder().encode(frame))
        close = () => controller.close()
      },
    }),
    { headers: { 'content-type': 'text/event-stream' } },
  )
  return { response, push: (frame) => push(frame), close: () => close() }
}

function setup(
  runtimeClient: RuntimeClient,
  agent: AgentDefinition,
  overrides: Partial<UseChatOptions> = {},
) {
  const scope = effectScope()
  const storage = memoryStorage()
  const checkpointStore = createCheckpointStore(storage)
  const result = scope.run(() =>
    useChat({
      config: config(agent),
      client: runtimeClient,
      checkpointStore,
      now: () => new Date('2026-08-04T10:00:00Z'),
      randomId: vi.fn().mockReturnValueOnce('session-1').mockReturnValue('message-id'),
      sleep: vi.fn().mockResolvedValue(undefined),
      autoBootstrap: false,
      ...overrides,
    }),
  )
  if (!result) throw new Error('failed to create composable')
  return { chat: result, checkpointStore, scope }
}

describe('useChat failure boundaries', () => {
  it('reads Todos on reconnect, write completion/failure and terminal events, but not tool starts', async () => {
    const read = vi
      .fn()
      .mockResolvedValue([{ content: '服务端清单', status: 'pending' }])
    const { chat, scope } = setup(client({ todos: read }), streamAgent)
    try {
      await chat.reconnect()
      await flushPromises()
      expect(read).toHaveBeenCalledTimes(1)
      const event: PublicEventEnvelope = {
        schema_version: 1,
        event_id: 'start',
        type: 'tool.started',
        session_id: 'session-1',
        created_at: '2026-09-29T00:00:00Z',
        payload: {
          tool_call_id: 'tool-1',
          tool_name: 'todo_write',
          arguments: { todos: [] },
        },
      }
      chat.applyEvent(event, '1-0')
      await flushPromises()
      expect(read).toHaveBeenCalledTimes(1)
      expect(chat.todos.items.value[0]?.content).toBe('服务端清单')
      for (const type of ['tool.completed', 'tool.failed', 'run.completed']) {
        chat.applyEvent({ ...event, type, event_id: type }, type)
        await flushPromises()
      }
      expect(read).toHaveBeenCalledTimes(4)
      await chat.createSession()
      expect(chat.todos.items.value).toEqual([])
    } finally {
      scope.stop()
    }
  })

  it('preserves a rejected deletion and does not hijack navigation when a retry finishes', async () => {
    const record = {
      sessionId: 'session-1',
      agentId: streamAgent.id,
      title: 'Saved',
      titleVersion: 1,
      createdAt: '2026-09-24T00:00:00Z',
      updatedAt: '2026-09-24T00:00:00Z',
    }
    let finish!: () => void
    const clear = vi.fn()
    const runtimeClient = client({
      sessions: vi.fn().mockResolvedValue({ sessions: [record], nextCursor: null }),
      deleteSession: vi
        .fn()
        .mockRejectedValueOnce(new Error('session is active'))
        .mockImplementation(
          () =>
            new Promise<void>((resolve) => {
              finish = resolve
            }),
        ),
    })
    const { chat, scope } = setup(runtimeClient, streamAgent, {
      checkpointStore: { load: () => null, save: () => {}, clear },
    })
    try {
      await chat.catalog.load()
      await chat.selectSession(record.sessionId, record.agentId)
      await chat.removeSession(record.sessionId, record.agentId)
      expect(chat.selectedSessionId.value).toBe(record.sessionId)
      expect(chat.sessions.value).toEqual([record])
      expect(clear).not.toHaveBeenCalled()
      const removing = chat.removeSession(record.sessionId, record.agentId)
      await chat.removeSession(record.sessionId, record.agentId)
      expect(runtimeClient.deleteSession).toHaveBeenCalledTimes(2)
      expect(await chat.send(textBlocks('blocked during deletion'))).toBe(false)
      await chat.createSession()
      const draft = chat.selectedSessionId.value
      finish()
      await removing
      expect(chat.sessions.value).toEqual([])
      expect(chat.selectedSessionId.value).toBe(draft)
      expect(clear).toHaveBeenCalledWith('["web","user","stream-agent","session-1"]')
    } finally {
      scope.stop()
    }
  })

  const agent: AgentDefinition = {
    id: 'agent',
    name: 'Agent',
    description: '',
    outputMode: 'stream',
  }

  it('continues projecting output when checkpoint persistence fails', async () => {
    const frame = (id: string, delta: string) =>
      `id: ${id}\ndata: ${JSON.stringify({ schema_version: 1, event_id: id, session_id: 'session-1', created_at: '2026-09-22T00:00:00Z', type: 'message.block.delta', payload: { block_id: 'b', block_type: 'text', delta } })}\n\n`
    const save = vi.fn().mockImplementation(() => {
      throw new Error('quota')
    })
    const { chat, scope } = setup(
      client({
        events: vi
          .fn()
          .mockResolvedValue(
            streamResponse(frame('e1', 'hello') + frame('e2', ' world')),
          ),
      }),
      agent,
      {
        checkpointStore: { load: () => null, save, clear: () => {} },
      },
    )
    try {
      expect(await chat.send(textBlocks('start'))).toBe(true)
      await vi.waitFor(() => expect(chat.isBusy.value).toBe(false))
      expect(chat.timeline.value.items).toEqual([
        expect.objectContaining({ content: 'hello world' }),
      ])
      expect(chat.timeline.value.cursor).toBe('e2')
      expect(chat.error.value).toBeNull()
      expect(chat.storageWarning.value).toBeTruthy()
      expect(save).toHaveBeenCalledOnce()
    } finally {
      scope.stop()
    }
  })

  it('keeps accepted messages and observes output when catalog refresh fails', async () => {
    const runtimeClient = client({
      sessions: vi.fn().mockRejectedValue(new Error('catalog offline')),
      events: vi
        .fn()
        .mockResolvedValue(streamResponse('event: stream.rotate\ndata: {}\n\n')),
    })
    const { chat, scope } = setup(runtimeClient, agent)
    try {
      expect(await chat.send(textBlocks('accepted'))).toBe(true)
      await vi.waitFor(() => expect(chat.isBusy.value).toBe(false))
      expect(chat.messages.value[0]?.content).toBe('accepted')
      expect(chat.error.value).toBeNull()
      expect(chat.catalog.error.value).toBe('catalog offline')
      expect(runtimeClient.events).toHaveBeenCalledOnce()
    } finally {
      scope.stop()
    }
  })

  it('exposes history failures instead of silently displaying an empty conversation', async () => {
    const { chat, scope } = setup(
      client({ messages: vi.fn().mockRejectedValue(new Error('history unavailable')) }),
      agent,
    )
    try {
      await chat.reconnect()
      expect(chat.error.value).toBe('history unavailable')
      expect(chat.isBusy.value).toBe(false)
    } finally {
      scope.stop()
    }
  })

  it('continues through normal rotations until authoritative completion', async () => {
    let connections = 0
    const runtimeClient = client({
      events: vi.fn().mockImplementation(async () => {
        connections += 1
        return streamResponse('event: stream.rotate\ndata: {}\n\n')
      }),
      snapshot: vi.fn().mockImplementation(async () => ({
        session_id: 'session-1',
        status: connections < 6 ? 'running' : 'succeeded',
      })),
    })
    const { chat, scope } = setup(runtimeClient, agent)
    try {
      await chat.reconnect()
      expect(chat.snapshot.value?.status).toBe('succeeded')
      expect(connections).toBe(6)
      expect(chat.error.value).toBeNull()
    } finally {
      scope.stop()
    }
  })

  it('reports repeated unexpected stream closure instead of silently stopping', async () => {
    const { chat, scope } = setup(
      client({
        events: vi.fn().mockImplementation(async () => streamResponse('')),
        snapshot: vi
          .fn()
          .mockResolvedValue({ session_id: 'session-1', status: 'running' }),
      }),
      agent,
    )
    try {
      await chat.reconnect()
      expect(chat.isRunning.value).toBe(true)
      expect(chat.error.value).toBeTruthy()
    } finally {
      scope.stop()
    }
  })

  it('does not exhaust retries on normal rotations while awaiting a newly accepted run', async () => {
    let connections = 0
    const { chat, scope } = setup(
      client({
        events: vi.fn().mockImplementation(async () => {
          connections += 1
          if (connections < 6)
            return streamResponse('event: stream.rotate\ndata: {}\n\n')
          return streamResponse(
            `id: new-run\ndata: ${JSON.stringify({ schema_version: 1, event_id: 'new-run', session_id: 'session-1', created_at: '2026-09-22T00:00:00Z', type: 'run.started', payload: {} })}\n\n`,
          )
        }),
        snapshot: vi
          .fn()
          .mockResolvedValue({ session_id: 'session-1', status: 'succeeded' }),
      }),
      agent,
    )
    try {
      expect(await chat.send(textBlocks('next run'))).toBe(true)
      await vi.waitFor(() => expect(chat.isBusy.value).toBe(false))
      expect(connections).toBe(6)
      expect(chat.error.value).toBeNull()
      expect(chat.snapshot.value?.status).toBe('succeeded')
    } finally {
      scope.stop()
    }
  })
})

describe('useChat Agent availability', () => {
  const availableAgent: AgentDefinition = {
    id: 'available-agent',
    name: 'Available',
    description: '',
    outputMode: 'complete',
  }

  async function initialize(records: SessionRecord[]) {
    const scope = effectScope()
    const storage = memoryStorage()
    const result = scope.run(() =>
      useChat({
        config: config(availableAgent),
        client: client({
          sessions: vi.fn().mockResolvedValue({ sessions: records, nextCursor: null }),
        }),
        checkpointStore: createCheckpointStore(storage),
        now: () => new Date('2026-08-04T10:00:00Z'),
        randomId: () => 'new-session',
        autoBootstrap: true,
      }),
    )
    if (!result) throw new Error('failed to create composable')
    await flushPromises()
    return { chat: result, scope }
  }

  it('ignores an unavailable Agent session when selecting the initial session', async () => {
    const { chat, scope } = await initialize([
      {
        sessionId: 'removed-session',
        agentId: 'removed-agent',
        title: 'Removed',
        titleVersion: 1,
        createdAt: '2026-08-04T08:00:00Z',
        updatedAt: '2026-08-04T10:00:00Z',
      },
      {
        sessionId: 'available-session',
        agentId: availableAgent.id,
        title: 'Available',
        titleVersion: 1,
        createdAt: '2026-08-04T07:00:00Z',
        updatedAt: '2026-08-04T09:00:00Z',
      },
    ])

    expect(chat.sessions.value.map((session) => session.sessionId)).toEqual([
      'available-session',
    ])
    expect(chat.selectedSessionId.value).toBe('available-session')
    expect(chat.selectedAgent.value.id).toBe(availableAgent.id)
    scope.stop()
  })

  it('creates a new session when every stored session uses an unavailable Agent', async () => {
    const { chat, scope } = await initialize([
      {
        sessionId: 'removed-session',
        agentId: 'removed-agent',
        title: 'Removed',
        titleVersion: 1,
        createdAt: '2026-08-04T08:00:00Z',
        updatedAt: '2026-08-04T10:00:00Z',
      },
    ])

    expect(chat.sessions.value).toEqual([])
    expect(chat.selectedSessionId.value).toBe('new-session')
    expect(chat.selectedAgent.value.id).toBe(availableAgent.id)
    scope.stop()
  })
})

describe('useChat transport selection', () => {
  it.each(['lookup-failure', 'attachment', 'system'] as const)(
    'keeps completion independent from naming for %s',
    async (scenario) => {
      const agent = { ...streamAgent, outputMode: 'complete' as const }
      const record = {
        sessionId: 'session-1',
        agentId: agent.id,
        title: 'Saved',
        titleVersion: 1,
        createdAt: '2026-09-24T00:00:00Z',
        updatedAt: '2026-09-24T00:00:00Z',
      }
      const history = [
        {
          message_id: 'message-id',
          role: scenario === 'system' ? 'system' : 'user',
          content: scenario === 'attachment' ? '' : 'Question',
          created_at: record.createdAt,
          is_error: false,
        },
        {
          message_id: 'answer',
          role: 'assistant',
          content: 'Answer',
          created_at: record.createdAt,
          is_error: false,
        },
      ]
      const messages = vi.fn().mockResolvedValue(history)
      if (scenario === 'lookup-failure')
        messages.mockRejectedValueOnce(new Error('context read failed'))
      const runtimeClient = client({
        sessions: vi.fn().mockResolvedValue({ sessions: [record], nextCursor: null }),
        messages,
        snapshot: vi
          .fn()
          .mockResolvedValueOnce({ session_id: 'session-1', status: 'running' })
          .mockResolvedValue({ session_id: 'session-1', status: 'succeeded' }),
      })
      const { chat, scope } = setup(runtimeClient, agent)
      try {
        await chat.send(
          scenario === 'attachment' ? imageBlocks() : textBlocks('Question'),
          { role: scenario === 'system' ? 'system' : 'user', origin: 'user' },
        )
        await flushPromises()
        expect(chat.snapshot.value?.status).toBe('succeeded')
        expect(chat.messages.value.at(-1)?.content).toBe('Answer')
        expect(chat.error.value).toBeNull()
        expect(runtimeClient.generateSessionTitle).not.toHaveBeenCalled()
        if (scenario === 'lookup-failure')
          expect(chat.catalog.error.value).toBe('context read failed')
      } finally {
        scope.stop()
      }
    },
  )

  it('tolerates the short 404 window before complete-mode session creation', async () => {
    const agent = { ...streamAgent, outputMode: 'complete' as const }
    const runtimeClient = client({
      snapshot: vi
        .fn()
        .mockRejectedValueOnce(
          new RuntimeHttpError(404, 'session_not_found', 'not ready'),
        )
        .mockResolvedValue({ session_id: 'session-1', status: 'succeeded' }),
      messages: vi.fn().mockResolvedValue([
        {
          message_id: 'message-id',
          role: 'user',
          content: 'Question',
          created_at: '2026-09-24T00:00:00Z',
          is_error: false,
        },
        {
          message_id: 'answer',
          role: 'assistant',
          content: 'Answer',
          created_at: '2026-09-24T00:00:00Z',
          is_error: false,
        },
      ]),
    })
    const { chat, scope } = setup(runtimeClient, agent)
    try {
      await chat.send(textBlocks('Question'))
      await flushPromises()
      expect(chat.snapshot.value?.status).toBe('succeeded')
      expect(chat.messages.value.at(-1)?.content).toBe('Answer')
      expect(chat.error.value).toBeNull()
    } finally {
      scope.stop()
    }
  })

  it('isolates checkpoint reads and deletion for equal session IDs in different agents', async () => {
    const other = { ...streamAgent, id: 'other' }
    const record = {
      sessionId: 'shared',
      agentId: streamAgent.id,
      title: 'Saved',
      titleVersion: 1,
      createdAt: '2026-09-24',
      updatedAt: '2026-09-24',
    }
    const load = vi.fn().mockReturnValue(null)
    const clear = vi.fn()
    const { chat, scope } = setup(
      client({
        sessions: vi.fn().mockResolvedValue({
          sessions: [record, { ...record, agentId: other.id }],
          nextCursor: null,
        }),
      }),
      streamAgent,
      {
        config: { ...config(streamAgent), agents: [streamAgent, other] },
        checkpointStore: { load, clear, save: () => {} },
      },
    )
    try {
      await chat.catalog.load()
      await chat.selectSession('shared', streamAgent.id)
      await chat.selectSession('shared', other.id)
      expect(load.mock.calls.map(([key]) => key)).toEqual([
        '["web","user","stream-agent","shared"]',
        '["web","user","other","shared"]',
      ])
      await chat.removeSession('shared', streamAgent.id)
      expect(clear).toHaveBeenCalledWith('["web","user","stream-agent","shared"]')
      expect(chat.selectedAgent.value.id).toBe('other')
    } finally {
      scope.stop()
    }
  })

  it('generates a title when the first message starts, before the answer completes', async () => {
    const record = {
      sessionId: 'session-1',
      agentId: 'stream-agent',
      title: 'Server title',
      titleVersion: 1,
      createdAt: '2026-09-24T00:00:00Z',
      updatedAt: '2026-09-24T00:00:00Z',
    }
    const runtimeClient = client({
      sessions: vi.fn().mockResolvedValue({ sessions: [record], nextCursor: null }),
      generateSessionTitle: vi
        .fn()
        .mockResolvedValue({ ...record, title: 'Generated title', titleVersion: 2 }),
      events: vi.fn().mockResolvedValue(pushableStreamResponse().response),
      snapshot: vi
        .fn()
        .mockResolvedValue({ session_id: 'session-1', status: 'running' }),
    })
    const { chat, scope } = setup(runtimeClient, streamAgent)
    try {
      expect(await chat.send(textBlocks('Question'))).toBe(true)
      await flushPromises()
      expect(runtimeClient.generateSessionTitle).not.toHaveBeenCalled()
      const started: PublicEventEnvelope = {
        schema_version: 1,
        event_id: 'started',
        type: 'message.started',
        session_id: 'session-1',
        created_at: record.createdAt,
        payload: {
          mode: 'queue',
          message: {
            message_id: 'message-id',
            role: 'user',
            origin: 'user',
            content: 'Question',
            created_at: record.createdAt,
            is_error: false,
          },
        },
      }
      chat.applyEvent(started, 'epoch:1-0')
      await vi.waitFor(() =>
        expect(chat.sessions.value[0]?.title).toBe('Generated title'),
      )
      expect(chat.isBusy.value).toBe(true)
      chat.applyEvent(started, 'epoch:1-0')
      expect(runtimeClient.generateSessionTitle).toHaveBeenCalledTimes(1)
    } finally {
      scope.stop()
    }
  })

  it('generates a title from persisted input while complete-mode execution is still running', async () => {
    const record = {
      sessionId: 'session-1',
      agentId: 'complete-agent',
      title: 'Question',
      titleVersion: 1,
      createdAt: '2026-09-24T00:00:00Z',
      updatedAt: '2026-09-24T00:00:00Z',
    }
    const runtimeClient = client({
      sessions: vi.fn().mockResolvedValue({ sessions: [record], nextCursor: null }),
      generateSessionTitle: vi
        .fn()
        .mockResolvedValue({ ...record, title: 'Generated', titleVersion: 2 }),
      snapshot: vi
        .fn()
        .mockResolvedValue({ session_id: 'session-1', status: 'running' }),
      messages: vi.fn().mockResolvedValue([
        {
          message_id: 'message-id',
          role: 'user',
          content: 'Question',
          created_at: record.createdAt,
          is_error: false,
        },
      ]),
    })
    const { chat, scope } = setup(
      runtimeClient,
      { ...streamAgent, id: 'complete-agent', outputMode: 'complete' },
      { sleep: () => new Promise(() => {}) },
    )
    try {
      await chat.send(textBlocks('Question'))
      await flushPromises()
      expect(chat.sessions.value[0]?.title).toBe('Generated')
      expect(chat.snapshot.value?.status).toBe('running')
      expect(runtimeClient.generateSessionTitle).toHaveBeenCalledOnce()
    } finally {
      scope.stop()
    }
  })

  it('refreshes the server catalog after acceptance without changing the selected Agent', async () => {
    const first: AgentDefinition = {
      id: 'first',
      name: 'First',
      description: '',
      outputMode: 'complete',
    }
    const second: AgentDefinition = { ...first, id: 'second', name: 'Second' }
    let accept!: () => void
    const runtimeClient = client({
      submitMessage: vi
        .fn()
        .mockRejectedValueOnce(new Error('rejected'))
        .mockImplementationOnce(
          () =>
            new Promise<void>((resolve) => {
              accept = resolve
            }),
        ),
    })
    const storage = memoryStorage()
    const scope = effectScope()
    const chat = scope.run(() =>
      useChat({
        config: { ...config(first), agents: [first, second] },
        client: runtimeClient,
        checkpointStore: createCheckpointStore(storage),
      }),
    )!
    expect(chat.sessions.value).toEqual([])
    expect(runtimeClient.snapshot).not.toHaveBeenCalled()
    await chat.selectAgent('second')
    await chat.createSession()
    expect(chat.selectedAgent.value.id).toBe('second')
    expect(chat.sessions.value).toEqual([])
    expect(await chat.send(textBlocks('Rejected'))).toBe(false)
    const sentSessionId = chat.selectedSessionId.value
    vi.mocked(runtimeClient.sessions).mockResolvedValue({
      sessions: [
        {
          sessionId: sentSessionId,
          agentId: 'second',
          title: 'Server-selected title',
          titleVersion: 1,
          createdAt: '2026-09-24T00:00:00Z',
          updatedAt: '2026-09-24T00:00:00Z',
        },
      ],
      nextCursor: null,
    })
    const sending = chat.send(textBlocks('Actual conversation'))
    expect(chat.sessions.value).toEqual([])
    await chat.selectAgent('first')
    accept()
    expect(await sending).toBe(true)
    await flushPromises()
    expect(chat.selectedAgent.value.id).toBe('first')
    expect(chat.sessions.value).toEqual([
      expect.objectContaining({
        sessionId: sentSessionId,
        agentId: 'second',
        title: 'Server-selected title',
      }),
    ])
    scope.stop()
  })

  it('submits ordered blocks and returns after Runtime accepts the message', async () => {
    const runtimeClient = client({
      events: vi
        .fn()
        .mockResolvedValue(streamResponse('event: stream.rotate\ndata: {}\n\n')),
    })
    const { chat, scope } = setup(runtimeClient, streamAgent)

    const accepted = await chat.send(imageBlocks('描述图片'), {
      role: 'system',
      origin: 'order.workflow',
    })

    expect(accepted).toBe(true)
    expect(runtimeClient.submitMessage).toHaveBeenCalledWith(
      expect.anything(),
      expect.objectContaining({
        role: 'system',
        origin: 'order.workflow',
        content: imageBlocks('描述图片'),
        mode: 'queue',
      }),
      expect.any(AbortSignal),
    )
    expect(chat.messages.value[0]).toMatchObject({
      role: 'system',
      origin: 'order.workflow',
    })
    expect(chat.messages.value[0]?.blocks).toEqual([
      {
        type: 'image',
        source_type: 'asset',
        media_type: 'image/png',
        filename: 'image.png',
        asset_id: 'asset-1',
      },
      { type: 'text', text: '描述图片' },
    ])
    scope.stop()
  })

  it('queues system steer media without opening another SSE stream', async () => {
    const runtimeClient = client({})
    const { chat, scope } = setup(runtimeClient, streamAgent)
    chat.snapshot.value = { session_id: 'session-1', status: 'running' }
    chat.isBusy.value = true
    chat.submissionMode.value = 'steer'

    const accepted = await chat.send(imageBlocks('优先处理退款'), {
      role: 'system',
      origin: 'human.console',
    })

    expect(accepted).toBe(true)
    expect(runtimeClient.submitMessage).toHaveBeenCalledWith(
      expect.anything(),
      expect.objectContaining({
        mode: 'steer',
        role: 'system',
        origin: 'human.console',
      }),
      expect.any(AbortSignal),
    )
    expect(runtimeClient.events).not.toHaveBeenCalled()
    expect(chat.isBusy.value).toBe(true)
    expect(chat.messages.value).toEqual([])
    expect(chat.pendingInputs.value).toHaveLength(1)
    expect(chat.pendingInputs.value[0]).toEqual(
      expect.objectContaining({
        messageId: 'message-id',
        content: '优先处理退款',
        role: 'system',
        origin: 'human.console',
        mode: 'steer',
        blocks: [
          expect.objectContaining({ type: 'image', asset_id: 'asset-1' }),
          { type: 'text', text: '优先处理退款' },
        ],
      }),
    )
    scope.stop()
  })

  it('uses the server title for a pure attachment message', async () => {
    const runtimeClient = client({
      events: vi
        .fn()
        .mockResolvedValue(streamResponse('event: stream.rotate\ndata: {}\n\n')),
    })
    const { chat, scope } = setup(runtimeClient, streamAgent)

    vi.mocked(runtimeClient.sessions).mockResolvedValue({
      sessions: [
        {
          sessionId: 'session-1',
          agentId: 'stream-agent',
          title: 'Server attachment title',
          titleVersion: 1,
          createdAt: '2026-09-24T00:00:00Z',
          updatedAt: '2026-09-24T00:00:00Z',
        },
      ],
      nextCursor: null,
    })
    await chat.send(imageBlocks())
    await flushPromises()

    expect(runtimeClient.submitMessage).toHaveBeenCalledWith(
      expect.anything(),
      expect.objectContaining({ role: 'user', origin: 'user' }),
      expect.any(AbortSignal),
    )
    expect(chat.sessions.value[0]?.title).toBe('Server attachment title')
    scope.stop()
  })

  it('reconstructs a failed terminal summary from the authoritative snapshot', async () => {
    const runtimeClient = client({
      snapshot: vi.fn().mockResolvedValue({
        session_id: 'session-1',
        status: 'failed',
        result: {
          error_code: 'context_compaction_failed',
          error_message: 'Context compaction failed',
        },
        usage: { input_tokens: 10, output_tokens: 0, total_tokens: 10 },
        timing: { elapsed_ms: 1200, active_ms: 900 },
      }),
    })
    const { chat, scope } = setup(runtimeClient, streamAgent)

    await chat.reconnect()

    expect(chat.timeline.value.terminal).toEqual({
      status: 'failed',
      error: {
        code: 'context_compaction_failed',
        message: 'Context compaction failed',
      },
      usage: { input_tokens: 10, output_tokens: 0, total_tokens: 10 },
      timing: { elapsed_ms: 1200, active_ms: 900 },
    })
    scope.stop()
  })

  it('uses SSE for stream agents and finalizes from authoritative messages', async () => {
    const events = vi
      .fn()
      .mockResolvedValue(
        streamResponse(
          'id: epoch:1-0\nevent: run.started\ndata: {"schema_version":1,"event_id":"started","type":"run.started","created_at":"2026-08-04T10:00:00Z","session_id":"session-1","payload":{"status":"running"}}\n\nid: epoch:2-0\nevent: run.completed\ndata: {"schema_version":1,"event_id":"done","type":"run.completed","created_at":"2026-08-04T10:00:00Z","session_id":"session-1","payload":{"status":"succeeded","usage":{"input_tokens":1,"output_tokens":2,"total_tokens":3},"timing":{"elapsed_ms":20,"active_ms":18},"snapshot_required":true}}\n\n',
        ),
      )
    const runtimeClient = client({
      events,
      snapshot: vi.fn().mockResolvedValue({
        session_id: 'session-1',
        status: 'succeeded',
      }),
      messages: vi.fn().mockResolvedValue([
        {
          message_id: 'answer',
          role: 'assistant',
          content: '完成',
          created_at: '2026-08-04T10:00:01Z',
          is_error: false,
        },
      ]),
    })
    const { chat, checkpointStore, scope } = setup(runtimeClient, streamAgent)

    await chat.send(textBlocks('请开始'))

    await vi.waitFor(() => {
      expect(chat.messages.value.at(-1)?.content).toBe('完成')
    })
    expect(events).toHaveBeenCalledOnce()
    expect(chat.timeline.value.terminal?.status).toBe('succeeded')
    expect(checkpointStore.load('["web","user","stream-agent","session-1"]')).toEqual({
      cursor: 'epoch:2-0',
      appliedEventIds: [],
    })
    scope.stop()
  })

  it('polls complete agents without opening SSE', async () => {
    const events = vi.fn()
    const snapshots: SessionSnapshot[] = [
      { session_id: 'session-1', status: 'running' },
      { session_id: 'session-1', status: 'succeeded' },
      { session_id: 'session-1', status: 'succeeded' },
    ]
    const runtimeClient = client({
      events,
      snapshot: vi.fn().mockImplementation(() => Promise.resolve(snapshots.shift())),
      messages: vi.fn().mockResolvedValue([
        {
          message_id: 'message-id',
          role: 'user',
          content: '完整回答',
          created_at: '2026-08-04T10:00:00Z',
          is_error: false,
        },
        {
          message_id: 'assistant-complete',
          role: 'assistant',
          content: '完成',
          created_at: '2026-08-04T10:00:01Z',
          is_error: false,
        },
      ]),
    })
    const { chat, scope } = setup(runtimeClient, {
      id: 'complete-agent',
      name: 'Complete',
      description: '',
      outputMode: 'complete',
    })

    await chat.send(textBlocks('完整回答'))

    await vi.waitFor(() => {
      expect(runtimeClient.snapshot).toHaveBeenCalledTimes(2)
    })
    expect(events).not.toHaveBeenCalled()
    scope.stop()
  })

  it('polls a complete cancellation to its authoritative canceled snapshot', async () => {
    const cancel = vi.fn().mockResolvedValue(undefined)
    const runtimeClient = client({
      cancel,
      snapshot: vi.fn().mockResolvedValue({
        session_id: 'session-1',
        status: 'canceled',
        result: {
          error_code: 'run_canceled',
          error_message: 'run 已被用户取消',
        },
        usage: { input_tokens: 3, output_tokens: 1, total_tokens: 4 },
        timing: { elapsed_ms: 20, active_ms: 18 },
      }),
      messages: vi.fn().mockResolvedValue([]),
      pendingInputs: vi.fn().mockResolvedValue([]),
    })
    const { chat, scope } = setup(runtimeClient, {
      id: 'complete-agent',
      name: 'Complete',
      description: '',
      outputMode: 'complete',
    })
    chat.snapshot.value = { session_id: 'session-1', status: 'running' }
    chat.isBusy.value = true

    await chat.cancel()

    expect(cancel).toHaveBeenCalledWith(
      expect.objectContaining({ sessionId: 'session-1' }),
      expect.any(AbortSignal),
    )
    expect(chat.snapshot.value?.status).toBe('canceled')
    expect(chat.timeline.value.terminal).toEqual(
      expect.objectContaining({
        status: 'canceled',
        error: expect.objectContaining({ code: 'run_canceled' }),
      }),
    )
    expect(chat.isBusy.value).toBe(false)
    scope.stop()
  })

  it('consumes run.canceled after a streaming cancellation is accepted', async () => {
    const canceled = {
      session_id: 'session-1',
      status: 'canceled' as const,
      result: {
        error_code: 'run_canceled',
        error_message: 'run 已被用户取消',
      },
      usage: { input_tokens: 3, output_tokens: 1, total_tokens: 4 },
      timing: { elapsed_ms: 20, active_ms: 18 },
    }
    const runtimeClient = client({
      cancel: vi.fn().mockResolvedValue(undefined),
      events: vi
        .fn()
        .mockResolvedValue(
          streamResponse(
            'id: epoch:2-0\nevent: run.canceled\ndata: {"schema_version":1,"event_id":"canceled-1","type":"run.canceled","created_at":"2026-08-04T10:00:01Z","session_id":"session-1","payload":{"status":"canceled","error":{"code":"run_canceled","message":"run 已被用户取消","retryable":false},"usage":{"input_tokens":3,"output_tokens":1,"total_tokens":4},"timing":{"elapsed_ms":20,"active_ms":18},"snapshot_required":true}}\n\n',
          ),
        ),
      snapshot: vi.fn().mockResolvedValue(canceled),
      messages: vi.fn().mockResolvedValue([]),
    })
    const { chat, scope } = setup(runtimeClient, streamAgent)
    chat.snapshot.value = { session_id: 'session-1', status: 'running' }
    chat.isBusy.value = true

    await chat.cancel()

    expect(runtimeClient.events).toHaveBeenCalledOnce()
    expect(chat.snapshot.value?.status).toBe('canceled')
    expect(chat.timeline.value.terminal).toEqual(
      expect.objectContaining({
        status: 'canceled',
        error: expect.objectContaining({ code: 'run_canceled' }),
      }),
    )
    expect(chat.isBusy.value).toBe(false)
    scope.stop()
  })

  it('ignores a previous complete terminal after accepting a new message', async () => {
    const oldTerminal: SessionSnapshot = {
      session_id: 'session-1',
      status: 'succeeded',
      timing: {
        started_at: '2026-08-04T09:59:00Z',
        ended_at: '2026-08-04T09:59:10Z',
        elapsed_ms: 10_000,
        active_ms: 10_000,
      },
    }
    const newTerminal: SessionSnapshot = {
      session_id: 'session-1',
      status: 'succeeded',
      timing: {
        started_at: '2026-08-04T10:00:01Z',
        ended_at: '2026-08-04T10:00:02Z',
        elapsed_ms: 1_000,
        active_ms: 1_000,
      },
    }
    const previousMessages = [
      {
        message_id: 'assistant-old',
        role: 'assistant' as const,
        content: '上一轮答案',
        created_at: '2026-08-04T09:59:10Z',
        is_error: false,
      },
    ]
    const runtimeClient = client({
      snapshot: vi
        .fn()
        .mockResolvedValueOnce(oldTerminal)
        .mockResolvedValueOnce({ session_id: 'session-1', status: 'running' })
        .mockResolvedValueOnce(newTerminal),
      messages: vi
        .fn()
        .mockResolvedValueOnce(previousMessages)
        .mockResolvedValueOnce([
          ...previousMessages,
          {
            message_id: 'message-id',
            role: 'user',
            content: '基本面',
            created_at: '2026-08-04T10:00:00Z',
            is_error: false,
          },
          {
            message_id: 'assistant-new',
            role: 'assistant',
            content: '新的基本面回答',
            created_at: '2026-08-04T10:00:02Z',
            is_error: false,
          },
        ]),
      pendingInputs: vi
        .fn()
        .mockResolvedValueOnce([
          {
            messageId: 'message-id',
            content: '基本面',
            blocks: [{ type: 'text', text: '基本面' }],
            mode: 'queue',
            createdAt: '2026-08-04T10:00:00Z',
          },
        ])
        .mockResolvedValueOnce([]),
    })
    const { chat, scope } = setup(runtimeClient, {
      id: 'complete-agent',
      name: 'Complete',
      description: '',
      outputMode: 'complete',
    })
    chat.snapshot.value = oldTerminal

    await chat.send(textBlocks('基本面'))

    await vi.waitFor(() => {
      expect(runtimeClient.snapshot).toHaveBeenCalledTimes(3)
    })
    expect(runtimeClient.pendingInputs).toHaveBeenCalledTimes(2)
    expect(chat.messages.value.at(-1)?.content).toBe('新的基本面回答')
    expect(chat.snapshot.value?.timing?.started_at).toBe('2026-08-04T10:00:01Z')
    scope.stop()
  })

  it('continues complete polling for a queued message after the current run ends', async () => {
    const oldTerminal: SessionSnapshot = {
      session_id: 'session-1',
      status: 'succeeded',
      timing: {
        started_at: '2026-08-04T09:59:00Z',
        ended_at: '2026-08-04T09:59:10Z',
        elapsed_ms: 10_000,
        active_ms: 10_000,
      },
    }
    const queuedInput = {
      messageId: 'message-id',
      content: '当前运行结束后执行',
      blocks: [{ type: 'text' as const, text: '当前运行结束后执行' }],
      mode: 'queue' as const,
      createdAt: '2026-08-04T10:00:00Z',
    }
    const previousMessages = [
      {
        message_id: 'assistant-old',
        role: 'assistant' as const,
        content: '上一轮答案',
        created_at: '2026-08-04T09:59:10Z',
        is_error: false,
      },
    ]
    const runtimeClient = client({
      snapshot: vi
        .fn()
        .mockResolvedValueOnce(oldTerminal)
        .mockResolvedValueOnce(oldTerminal)
        .mockResolvedValueOnce({ session_id: 'session-1', status: 'running' })
        .mockResolvedValueOnce({
          session_id: 'session-1',
          status: 'succeeded',
          timing: {
            started_at: '2026-08-04T10:00:01Z',
            ended_at: '2026-08-04T10:00:02Z',
            elapsed_ms: 1_000,
            active_ms: 1_000,
          },
        }),
      messages: vi
        .fn()
        .mockResolvedValueOnce(previousMessages)
        .mockResolvedValueOnce(previousMessages)
        .mockResolvedValueOnce([
          ...previousMessages,
          {
            message_id: 'message-id',
            role: 'user',
            content: queuedInput.content,
            created_at: queuedInput.createdAt,
            is_error: false,
          },
          {
            message_id: 'assistant-new',
            role: 'assistant',
            content: '队列消息回答',
            created_at: '2026-08-04T10:00:02Z',
            is_error: false,
          },
        ]),
      pendingInputs: vi
        .fn()
        .mockResolvedValueOnce([queuedInput])
        .mockResolvedValueOnce([queuedInput])
        .mockResolvedValueOnce([]),
    })
    const { chat, scope } = setup(runtimeClient, {
      id: 'complete-agent',
      name: 'Complete',
      description: '',
      outputMode: 'complete',
    })
    chat.snapshot.value = { session_id: 'session-1', status: 'running' }

    await chat.send(textBlocks(queuedInput.content))
    await chat.reconnect()

    expect(runtimeClient.snapshot).toHaveBeenCalledTimes(4)
    expect(chat.pendingInputs.value).toEqual([])
    expect(chat.messages.value.at(-1)?.content).toBe('队列消息回答')
    scope.stop()
  })

  it('preserves steer when selected without a running session', async () => {
    const runtimeClient = client({
      events: vi
        .fn()
        .mockResolvedValue(streamResponse('event: stream.rotate\ndata: {}\n\n')),
    })
    const { chat, scope } = setup(runtimeClient, streamAgent)
    chat.submissionMode.value = 'steer'

    await chat.send(textBlocks('新问题'))

    expect(runtimeClient.submitMessage).toHaveBeenCalledWith(
      expect.anything(),
      expect.objectContaining({ mode: 'steer' }),
      expect.any(AbortSignal),
    )
    expect(chat.submissionMode.value).toBe('steer')
    expect(chat.pendingInputs.value).toEqual([])
    expect(chat.messages.value).toEqual([
      expect.objectContaining({ message_id: 'message-id', content: '新问题' }),
    ])
    scope.stop()
  })

  it('rolls back rejected media while preserving the previous run projection', async () => {
    const runtimeClient = client({
      submitMessage: vi.fn().mockRejectedValue(new Error('network failed')),
    })
    const { chat, scope } = setup(runtimeClient, streamAgent)
    chat.snapshot.value = { session_id: 'session-1', status: 'succeeded' }
    chat.timeline.value = {
      items: [],
      appliedEventIds: ['done-1'],
      terminal: {
        status: 'succeeded',
        usage: { input_tokens: 1, output_tokens: 2, total_tokens: 3 },
      },
    }

    expect(await chat.send(imageBlocks('下一轮'))).toBe(false)

    expect(chat.timeline.value.terminal?.usage?.total_tokens).toBe(3)
    expect(chat.messages.value).toEqual([])
    expect(chat.error.value).toBe('network failed')
    scope.stop()
  })

  it('restores non-running pending input directly into chat', async () => {
    const runtimeClient = client({
      snapshot: vi.fn().mockResolvedValue({
        session_id: 'session-1',
        status: 'succeeded',
      }),
      messages: vi
        .fn()
        .mockResolvedValueOnce([])
        .mockResolvedValue([
          {
            message_id: 'pending-1',
            role: 'user',
            content: '稍后执行',
            blocks: [
              {
                type: 'image',
                source_type: 'asset',
                media_type: 'image/png',
                filename: 'image.png',
                asset_id: 'asset-1',
              },
              { type: 'text', text: '稍后执行' },
            ],
            created_at: '2026-08-04T10:00:00Z',
            is_error: false,
          },
        ]),
      pendingInputs: vi.fn().mockResolvedValue([
        {
          messageId: 'pending-1',
          content: '稍后执行',
          blocks: [
            {
              type: 'image',
              source_type: 'asset',
              media_type: 'image/png',
              filename: 'image.png',
              asset_id: 'asset-1',
            },
            { type: 'text', text: '稍后执行' },
          ],
          mode: 'queue',
          createdAt: '2026-08-04T10:00:00Z',
        },
      ]),
    })
    const { chat, scope } = setup(runtimeClient, {
      id: 'complete-agent',
      name: 'Complete',
      description: '',
      outputMode: 'complete',
    })

    await chat.reconnect()

    expect(chat.pendingInputs.value).toEqual([])
    expect(chat.messages.value).toEqual([
      expect.objectContaining({
        message_id: 'pending-1',
        content: '稍后执行',
        blocks: [
          expect.objectContaining({ type: 'image', asset_id: 'asset-1' }),
          { type: 'text', text: '稍后执行' },
        ],
      }),
    ])
    scope.stop()
  })

  it('restores running inputs in the pending area', async () => {
    const runtimeClient = client({
      snapshot: vi
        .fn()
        .mockResolvedValueOnce({
          session_id: 'session-1',
          status: 'running',
        })
        .mockResolvedValue({
          session_id: 'session-1',
          status: 'succeeded',
        }),
      pendingInputs: vi.fn().mockResolvedValue([
        {
          messageId: 'pending-1',
          content: '稍后执行',
          blocks: [
            {
              type: 'image',
              source_type: 'asset',
              media_type: 'image/png',
              filename: 'image.png',
              asset_id: 'asset-1',
            },
            { type: 'text', text: '稍后执行' },
          ],
          mode: 'queue',
          createdAt: '2026-08-04T10:00:00Z',
        },
      ]),
      events: vi.fn().mockResolvedValue(streamResponse('')),
    })
    const { chat, scope } = setup(runtimeClient, streamAgent)

    await chat.reconnect()

    expect(chat.pendingInputs.value).toEqual([
      expect.objectContaining({
        messageId: 'pending-1',
        mode: 'queue',
        blocks: [
          expect.objectContaining({ type: 'image', asset_id: 'asset-1' }),
          { type: 'text', text: '稍后执行' },
        ],
      }),
    ])
    scope.stop()
  })

  it('does not re-add pending when message.started arrives before HTTP 202', async () => {
    let accept = (): void => {
      throw new Error('submission is not ready')
    }
    const submitMessage = vi.fn().mockImplementation(
      () =>
        new Promise<void>((resolve) => {
          accept = resolve
        }),
    )
    const runtimeClient = client({ submitMessage })
    const { chat, scope } = setup(runtimeClient, streamAgent)
    chat.snapshot.value = { session_id: 'session-1', status: 'running' }
    chat.isBusy.value = true

    const sending = chat.send(textBlocks('插入当前运行'))
    chat.applyEvent(
      {
        schema_version: 1,
        event_id: 'started-1',
        type: 'message.started',
        created_at: '2026-08-04T10:00:00Z',
        session_id: 'session-1',
        payload: {
          mode: 'queue',
          message: {
            message_id: 'message-id',
            role: 'user',
            origin: 'user',
            content: '插入当前运行',
            blocks: [{ type: 'text', text: '插入当前运行' }],
            created_at: '2026-08-04T10:00:00Z',
            is_error: false,
          },
        },
      },
      'epoch:1-0',
    )
    accept()
    await sending

    expect(chat.messages.value.map((message) => message.message_id)).toEqual([
      'message-id',
    ])
    expect(chat.pendingInputs.value).toEqual([])
    scope.stop()
  })

  it('retries the short 404 window between message acceptance and stream creation', async () => {
    const events = vi
      .fn()
      .mockRejectedValueOnce(
        new RuntimeHttpError(404, 'session_not_found', 'not ready'),
      )
      .mockResolvedValueOnce(
        streamResponse(
          'id: epoch:1-0\nevent: run.started\ndata: {"schema_version":1,"event_id":"started","type":"run.started","created_at":"2026-08-04T10:00:00Z","session_id":"session-1","payload":{"status":"running"}}\n\nid: epoch:2-0\nevent: run.completed\ndata: {"schema_version":1,"event_id":"done","type":"run.completed","created_at":"2026-08-04T10:00:00Z","session_id":"session-1","payload":{"status":"succeeded","usage":{"input_tokens":1,"output_tokens":1,"total_tokens":2},"timing":{"elapsed_ms":10,"active_ms":9},"snapshot_required":true}}\n\n',
        ),
      )
    const runtimeClient = client({
      events,
      snapshot: vi.fn().mockResolvedValue({
        session_id: 'session-1',
        status: 'succeeded',
      }),
    })
    const { chat, scope } = setup(runtimeClient, streamAgent)

    await chat.send(textBlocks('触发竞态'))

    await vi.waitFor(() => {
      expect(events).toHaveBeenCalledTimes(2)
    })
    expect(chat.error.value).toBeNull()
    scope.stop()
  })

  it('keeps the first optimistic user message during stream recovery', async () => {
    const activeStream = controlledStreamResponse(
      'id: epoch:1-0\nevent: run.started\ndata: {"schema_version":1,"event_id":"started","type":"run.started","created_at":"2026-08-04T10:00:00Z","session_id":"session-1","payload":{"status":"running"}}\n\nid: epoch:2-0\nevent: message.block.completed\ndata: {"schema_version":1,"event_id":"thinking","type":"message.block.completed","created_at":"2026-08-04T10:00:00Z","session_id":"session-1","payload":{"message_id":"assistant","block_id":"thinking","block_type":"thinking","content":"正在处理"}}\n\n',
      'id: epoch:3-0\nevent: run.completed\ndata: {"schema_version":1,"event_id":"done","type":"run.completed","created_at":"2026-08-04T10:00:01Z","session_id":"session-1","payload":{"status":"succeeded","usage":{"input_tokens":1,"output_tokens":1,"total_tokens":2},"timing":{"elapsed_ms":10,"active_ms":9},"snapshot_required":true}}\n\n',
    )
    const events = vi
      .fn()
      .mockRejectedValueOnce(
        new RuntimeHttpError(409, 'snapshot_required', 'not ready'),
      )
      .mockResolvedValueOnce(activeStream.response)
    const runtimeClient = client({
      events,
      snapshot: vi.fn().mockResolvedValue({
        session_id: 'session-1',
        status: 'succeeded',
      }),
      messages: vi
        .fn()
        .mockResolvedValueOnce([])
        .mockResolvedValue([
          {
            message_id: 'message-id',
            role: 'user',
            content: '第一条消息',
            created_at: '2026-08-04T10:00:00Z',
            is_error: false,
          },
          {
            message_id: 'answer',
            role: 'assistant',
            content: '完成',
            created_at: '2026-08-04T10:00:01Z',
            is_error: false,
          },
        ]),
    })
    const { chat, scope } = setup(runtimeClient, streamAgent)

    const sending = chat.send(textBlocks('第一条消息'))
    await vi.waitFor(() => {
      expect(chat.timeline.value.items).toEqual([
        expect.objectContaining({ kind: 'block', content: '正在处理' }),
      ])
    })
    expect(chat.isSending.value).toBe(false)
    expect(chat.messages.value.map((message) => message.content)).toEqual([
      '第一条消息',
    ])

    activeStream.finish()
    await sending
    await vi.waitFor(() => {
      expect(chat.messages.value.map((message) => message.content)).toEqual([
        '第一条消息',
        '完成',
      ])
    })
    scope.stop()
  })

  it('clears the previous terminal projection before consuming a second run', async () => {
    const second = controlledStreamResponse(
      'id: epoch:2-0\nevent: run.started\ndata: {"schema_version":1,"event_id":"started-2","type":"run.started","created_at":"2026-08-04T10:01:00Z","session_id":"session-1","payload":{"status":"running"}}\n\nid: epoch:3-0\nevent: message.block.completed\ndata: {"schema_version":1,"event_id":"thinking-2","type":"message.block.completed","created_at":"2026-08-04T10:01:00Z","session_id":"session-1","payload":{"message_id":"assistant-2","block_id":"thinking-2","block_type":"thinking","content":"第二轮思考"}}\n\n',
      'id: epoch:4-0\nevent: run.completed\ndata: {"schema_version":1,"event_id":"done-2","type":"run.completed","created_at":"2026-08-04T10:01:01Z","session_id":"session-1","payload":{"status":"succeeded","usage":{"input_tokens":4,"output_tokens":5,"total_tokens":9},"timing":{"elapsed_ms":30,"active_ms":25},"snapshot_required":true}}\n\n',
    )
    const events = vi
      .fn()
      .mockResolvedValueOnce(
        streamResponse(
          'id: epoch:1-0\nevent: run.started\ndata: {"schema_version":1,"event_id":"started-1","type":"run.started","created_at":"2026-08-04T10:00:00Z","session_id":"session-1","payload":{"status":"running"}}\n\nid: epoch:2-0\nevent: run.completed\ndata: {"schema_version":1,"event_id":"done-1","type":"run.completed","created_at":"2026-08-04T10:00:00Z","session_id":"session-1","payload":{"status":"succeeded","usage":{"input_tokens":1,"output_tokens":2,"total_tokens":3},"timing":{"elapsed_ms":20,"active_ms":18},"snapshot_required":true}}\n\n',
        ),
      )
      .mockResolvedValueOnce(second.response)
    const runtimeClient = client({
      events,
      snapshot: vi.fn().mockResolvedValue({
        session_id: 'session-1',
        status: 'succeeded',
      }),
      messages: vi.fn().mockResolvedValue([]),
    })
    const { chat, scope } = setup(runtimeClient, streamAgent)

    await chat.send(textBlocks('第一轮'))
    await vi.waitFor(() => {
      expect(chat.timeline.value.terminal?.usage?.total_tokens).toBe(3)
    })

    // 上一条测试 Stream 已 EOF，新输入应从保留的 boundary cursor 续连。
    chat.snapshot.value = { session_id: 'session-1', status: 'succeeded' }

    const secondRun = chat.send(textBlocks('第二轮'))
    await vi.waitFor(() => {
      expect(chat.timeline.value.items).toEqual([
        expect.objectContaining({ kind: 'block', content: '第二轮思考' }),
      ])
    })
    expect(chat.timeline.value.terminal).toBeUndefined()
    expect(events.mock.calls[1]?.[1]).toBe('epoch:2-0')
    expect(runtimeClient.submitMessage).toHaveBeenLastCalledWith(
      expect.anything(),
      expect.objectContaining({ mode: 'queue' }),
      expect.any(AbortSignal),
    )

    second.finish()
    await secondRun
    expect(chat.timeline.value.terminal?.usage?.total_tokens).toBe(9)
    scope.stop()
  })

  it('ignores the previous terminal snapshot while an accepted run is starting', async () => {
    const nextRun = [
      'id: epoch:2-0',
      'event: run.started',
      'data: {"schema_version":1,"event_id":"started-2","type":"run.started","created_at":"2026-08-04T10:01:00Z","session_id":"session-1","payload":{}}',
      '',
      'id: epoch:3-0',
      'event: run.completed',
      'data: {"schema_version":1,"event_id":"done-2","type":"run.completed","created_at":"2026-08-04T10:01:01Z","session_id":"session-1","payload":{"status":"succeeded","usage":{"input_tokens":4,"output_tokens":5,"total_tokens":9},"timing":{"elapsed_ms":30,"active_ms":25},"snapshot_required":true}}',
      '',
      '',
    ].join('\n')
    const events = vi
      .fn()
      .mockResolvedValueOnce(streamResponse(''))
      .mockResolvedValueOnce(streamResponse(nextRun))
    const snapshot = vi
      .fn()
      .mockResolvedValueOnce({
        session_id: 'session-1',
        status: 'succeeded',
        usage: { input_tokens: 3, output_tokens: 4, total_tokens: 7 },
        timing: { elapsed_ms: 20, active_ms: 18 },
      })
      .mockResolvedValue({
        session_id: 'session-1',
        status: 'succeeded',
        usage: { input_tokens: 4, output_tokens: 5, total_tokens: 9 },
        timing: { elapsed_ms: 30, active_ms: 25 },
      })
    const runtimeClient = client({ events, snapshot })
    const { chat, checkpointStore, scope } = setup(runtimeClient, streamAgent)
    chat.snapshot.value = {
      session_id: 'session-1',
      status: 'succeeded',
      usage: { input_tokens: 3, output_tokens: 4, total_tokens: 7 },
      timing: { elapsed_ms: 20, active_ms: 18 },
    }
    chat.timeline.value = {
      items: [],
      appliedEventIds: ['done-1'],
      cursor: 'epoch:1-0',
      terminal: {
        status: 'succeeded',
        usage: { input_tokens: 3, output_tokens: 4, total_tokens: 7 },
        timing: { elapsed_ms: 20, active_ms: 18 },
      },
    }
    checkpointStore.save('["web","user","stream-agent","session-1"]', {
      cursor: 'epoch:1-0',
      appliedEventIds: [],
    })

    await chat.send(textBlocks('是吗'))

    await vi.waitFor(() => {
      expect(events).toHaveBeenCalledTimes(2)
    })
    expect(chat.timeline.value.terminal?.usage?.total_tokens).toBe(9)
    expect(chat.timeline.value.appliedEventIds).toEqual(['started-2', 'done-2'])
    scope.stop()
  })

  it('reconnects with the boundary cursor after an idle terminal EOF', async () => {
    const firstStream = pushableStreamResponse()
    const secondStream = pushableStreamResponse()
    const events = vi
      .fn()
      .mockResolvedValueOnce(firstStream.response)
      .mockResolvedValueOnce(secondStream.response)
    const submitMessage = vi.fn().mockResolvedValue(undefined)
    const snapshot = vi
      .fn()
      .mockResolvedValueOnce({ session_id: 'session-1', status: 'running' })
      .mockResolvedValue({ session_id: 'session-1', status: 'succeeded' })
    const runtimeClient = client({ events, snapshot, submitMessage })
    const { chat, scope } = setup(runtimeClient, streamAgent)

    const connected = chat.reconnect()
    await vi.waitFor(() => expect(events).toHaveBeenCalledOnce())
    firstStream.push(
      'id: epoch:1-0\nevent: run.completed\ndata: {"schema_version":1,"event_id":"done-1","type":"run.completed","created_at":"2026-08-04T10:00:00Z","session_id":"session-1","payload":{"status":"succeeded","usage":{"input_tokens":1,"output_tokens":2,"total_tokens":3},"timing":{"elapsed_ms":20,"active_ms":18},"snapshot_required":true}}\n\n',
    )
    await vi.waitFor(() => {
      expect(chat.snapshot.value?.status).toBe('succeeded')
      expect(chat.isBusy.value).toBe(false)
    })
    firstStream.close()
    await connected

    await chat.send(textBlocks('第二轮'))
    await vi.waitFor(() => expect(events).toHaveBeenCalledTimes(2))
    expect(chat.messages.value.at(-1)?.content).toBe('第二轮')
    expect(events.mock.calls[1]?.[1]).toBe('epoch:1-0')
    secondStream.push(
      'id: epoch:2-0\nevent: run.started\ndata: {"schema_version":1,"event_id":"run-started-2","type":"run.started","created_at":"2026-08-04T10:01:00Z","session_id":"session-1","payload":{"status":"running"}}\n\n',
    )
    await vi.waitFor(() => {
      expect(chat.snapshot.value?.status).toBe('running')
      expect(chat.timeline.value.terminal).toBeUndefined()
    })
    secondStream.push(
      'id: epoch:3-0\nevent: message.started\ndata: {"schema_version":1,"event_id":"started-2","type":"message.started","created_at":"2026-08-04T10:01:00Z","session_id":"session-1","payload":{"mode":"queue","message":{"message_id":"message-id","role":"user","content":"第二轮","blocks":[{"type":"text","text":"第二轮"}],"created_at":"2026-08-04T10:01:00Z","is_error":false}}}\n\n',
    )
    await vi.waitFor(() => {
      expect(chat.messages.value.at(-1)?.content).toBe('第二轮')
      expect(chat.timeline.value.terminal).toBeUndefined()
    })
    secondStream.push(
      'id: epoch:4-0\nevent: run.completed\ndata: {"schema_version":1,"event_id":"done-2","type":"run.completed","created_at":"2026-08-04T10:01:01Z","session_id":"session-1","payload":{"status":"succeeded","usage":{"input_tokens":2,"output_tokens":3,"total_tokens":5},"timing":{"elapsed_ms":30,"active_ms":25},"snapshot_required":true}}\n\n',
    )
    await vi.waitFor(() => expect(chat.isBusy.value).toBe(false))
    secondStream.close()

    expect(events).toHaveBeenCalledTimes(2)
    expect(chat.timeline.value.terminal?.usage?.total_tokens).toBe(5)
    scope.stop()
  })

  it('does not project an aborted session stream into a newly created chat', async () => {
    const stream = pushableStreamResponse()
    const runtimeClient = client({
      events: vi.fn().mockResolvedValue(stream.response),
      snapshot: vi.fn().mockResolvedValue({
        session_id: 'session-1',
        status: 'running',
      }),
    })
    const { chat, scope } = setup(runtimeClient, streamAgent)

    const reconnecting = chat.reconnect()
    await vi.waitFor(() => expect(runtimeClient.events).toHaveBeenCalledOnce())
    await chat.createSession()
    stream.push(
      'id: epoch:1-0\nevent: message.started\ndata: {"schema_version":1,"event_id":"old-started","type":"message.started","created_at":"2026-08-04T10:00:00Z","session_id":"session-1","payload":{"mode":"queue","message":{"message_id":"old-message","role":"user","content":"旧会话","blocks":[{"type":"text","text":"旧会话"}],"created_at":"2026-08-04T10:00:00Z","is_error":false}}}\n\n',
    )
    stream.close()
    await reconnecting

    expect(chat.messages.value).toEqual([])
    expect(chat.timeline.value.appliedEventIds).toEqual([])
    scope.stop()
  })
})
