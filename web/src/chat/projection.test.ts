import { describe, expect, it } from 'vitest'

import { applyMessageStarted, applyPublicEvent, emptyTimeline } from './projection'

function event(eventId: string, type: string, payload: Record<string, unknown>) {
  return {
    schema_version: 1 as const,
    event_id: eventId,
    type,
    created_at: '2026-08-04T09:00:00Z',
    session_id: 'session',
    payload,
  }
}

describe('applyPublicEvent', () => {
  it('projects context compaction start and completion as one timed item', () => {
    let timeline = emptyTimeline()
    timeline = applyPublicEvent(
      timeline,
      {
        ...event('compact-start', 'context.compaction.started', {}),
        created_at: '2026-08-04T09:00:00Z',
      },
      'epoch:1-0',
    )
    timeline = applyPublicEvent(
      timeline,
      {
        ...event('compact-end', 'context.compaction.completed', {
          message_id: 'compaction-message',
          action: 'summarized',
          before_tokens: 9000,
          after_tokens: 5000,
          usage: {},
        }),
        created_at: '2026-08-04T09:00:02Z',
      },
      'epoch:2-0',
    )

    expect(timeline.items).toEqual([
      expect.objectContaining({
        kind: 'compaction',
        messageId: 'compaction-message',
        status: 'completed',
        action: 'summarized',
        beforeTokens: 9000,
        afterTokens: 5000,
        durationMs: 2000,
      }),
    ])
  })

  it('marks an in-flight compaction failed when the run fails', () => {
    let timeline = applyPublicEvent(
      emptyTimeline(),
      event('compact-start', 'context.compaction.started', {}),
      'epoch:1-0',
    )
    timeline = applyPublicEvent(
      timeline,
      event('failed', 'run.failed', {
        status: 'failed',
        usage: {},
        timing: {},
      }),
      'epoch:2-0',
    )

    expect(timeline.items[0]).toMatchObject({
      kind: 'compaction',
      status: 'failed',
    })
  })

  it('moves a pending input into chat exactly once on message.started', () => {
    const started = event('started', 'message.started', {
      mode: 'steer',
      message: {
        message_id: 'message-1',
        role: 'system',
        origin: 'workflow.refund',
        content: '调整方向',
        blocks: [{ type: 'text', text: '调整方向' }],
        created_at: '2026-08-04T09:00:00Z',
        is_error: false,
      },
    })
    const initial = {
      messages: [],
      pendingInputs: [
        {
          messageId: 'message-1',
          role: 'system' as const,
          origin: 'workflow.refund',
          content: '调整方向',
          blocks: [{ type: 'text' as const, text: '调整方向' }],
          mode: 'steer' as const,
          createdAt: '2026-08-04T09:00:00Z',
        },
      ],
    }

    const moved = applyMessageStarted(initial, started)
    const replayed = applyMessageStarted(moved, started)

    expect(moved.pendingInputs).toEqual([])
    expect(moved.messages.map((message) => message.message_id)).toEqual(['message-1'])
    expect(moved.messages[0]).toMatchObject({
      role: 'system',
      origin: 'workflow.refund',
    })
    expect(replayed).toEqual(moved)
  })

  it('upserts an unknown started message into chat', () => {
    const started = event('started', 'message.started', {
      mode: 'queue',
      message: {
        message_id: 'message-2',
        role: 'user',
        origin: 'user',
        content: '下一个问题',
        blocks: [{ type: 'text', text: '下一个问题' }],
        created_at: '2026-08-04T09:00:01Z',
        is_error: false,
      },
    })

    const result = applyMessageStarted({ messages: [], pendingInputs: [] }, started)

    expect(result.messages).toHaveLength(1)
    expect(result.pendingInputs).toEqual([])
  })

  it('ignores inbound started messages without a string origin', () => {
    const missingOrigin = event('missing-origin', 'message.started', {
      mode: 'queue',
      message: {
        message_id: 'user-invalid',
        role: 'user',
        content: 'invalid',
        created_at: '2026-08-14T10:00:00Z',
        is_error: false,
      },
    })
    const numericOrigin = event('numeric-origin', 'message.started', {
      mode: 'queue',
      message: {
        message_id: 'system-invalid',
        role: 'system',
        origin: 42,
        content: 'invalid',
        created_at: '2026-08-14T10:00:00Z',
        is_error: false,
      },
    })
    const current = { messages: [], pendingInputs: [] }

    expect(applyMessageStarted(current, missingOrigin)).toBe(current)
    expect(applyMessageStarted(current, numericOrigin)).toBe(current)
  })

  it('applies block deltas in order and ignores duplicate event ids', () => {
    let timeline = emptyTimeline()
    timeline = applyPublicEvent(
      timeline,
      event('one', 'message.block.started', {
        message_id: 'assistant',
        block_id: 'text',
        index: 0,
        block_type: 'text',
      }),
      'epoch:1-0',
    )
    timeline = applyPublicEvent(
      timeline,
      event('two', 'message.block.delta', {
        message_id: 'assistant',
        block_id: 'text',
        index: 0,
        block_type: 'text',
        delta: '你好',
      }),
      'epoch:2-0',
    )
    timeline = applyPublicEvent(
      timeline,
      event('two', 'message.block.delta', {
        block_id: 'text',
        delta: '重复',
      }),
      'epoch:2-0',
    )

    expect(timeline.items.find((item) => item.kind === 'block')).toMatchObject({
      key: 'assistant-0',
      blockType: 'text',
      content: '你好',
    })
    expect(timeline.cursor).toBe('epoch:2-0')
  })

  it('preserves a started steer message as an ordered timeline boundary', () => {
    let timeline = emptyTimeline()
    timeline = applyPublicEvent(
      timeline,
      event('thinking', 'message.block.completed', {
        message_id: 'assistant-1',
        block_id: 'thinking-1',
        index: 0,
        block_type: 'thinking',
        content: '问题一思考',
      }),
      'epoch:1-0',
    )
    const steerStarted = event('steer-started', 'message.started', {
      mode: 'steer',
      message: {
        message_id: 'user-2',
        role: 'user',
        content: '问题二',
        blocks: [{ type: 'text', text: '问题二' }],
        created_at: '2026-08-04T08:59:00Z',
        is_error: false,
      },
    })
    timeline = applyPublicEvent(timeline, steerStarted, 'epoch:2-0')
    timeline = applyPublicEvent(
      timeline,
      event('answer', 'message.block.completed', {
        message_id: 'assistant-2',
        block_id: 'answer-2',
        index: 0,
        block_type: 'text',
        content: '问题二后的回答',
      }),
      'epoch:3-0',
    )
    timeline = applyPublicEvent(timeline, steerStarted, 'epoch:2-0')

    expect(timeline.items.map((item) => item.kind)).toEqual([
      'block',
      'message-boundary',
      'block',
    ])
    expect(timeline.items[1]).toMatchObject({
      kind: 'message-boundary',
      messageId: 'user-2',
    })
  })

  it('projects thinking, tool lifecycle and terminal metrics', () => {
    let timeline = emptyTimeline()
    timeline = applyPublicEvent(
      timeline,
      {
        ...event('thinking-start', 'message.block.started', {
          message_id: 'assistant',
          block_id: 'thinking',
          index: 0,
          block_type: 'thinking',
        }),
        created_at: '2026-08-04T10:00:00.000Z',
      },
      'epoch:1-0',
    )
    timeline = applyPublicEvent(
      timeline,
      {
        ...event('thinking-end', 'message.block.completed', {
          message_id: 'assistant',
          block_id: 'thinking',
          index: 0,
          block_type: 'thinking',
          content: '分析完成',
        }),
        created_at: '2026-08-04T10:00:01.200Z',
      },
      'epoch:2-0',
    )

    timeline = applyPublicEvent(
      timeline,
      event('tool-start', 'tool.started', {
        tool_call_id: 'call-1',
        tool_name: 'web_search',
        arguments: { query: 'Vue' },
      }),
      'epoch:3-0',
    )
    timeline = applyPublicEvent(
      timeline,
      event('tool-end', 'tool.completed', {
        tool_call_id: 'call-1',
        tool_name: 'web_search',
        content: 'result',
        is_error: false,
        duration_ms: 42,
      }),
      'epoch:4-0',
    )
    timeline = applyPublicEvent(
      timeline,
      event('done', 'run.completed', {
        status: 'succeeded',
        usage: { input_tokens: 8, output_tokens: 5, total_tokens: 13 },
        timing: { elapsed_ms: 1200, active_ms: 900 },
        snapshot_required: true,
      }),
      'epoch:5-0',
    )

    expect(timeline.items).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          kind: 'block',
          blockType: 'thinking',
          content: '分析完成',
          durationMs: 1200,
        }),
        expect.objectContaining({
          kind: 'tool',
          name: 'web_search',
          status: 'completed',
          durationMs: 42,
        }),
      ]),
    )
    expect(timeline.items.filter((item) => item.kind === 'tool')).toHaveLength(1)
    expect(timeline.terminal).toMatchObject({
      status: 'succeeded',
      usage: { total_tokens: 13 },
    })
  })

  it('does not add run, turn or model bookkeeping to the visible timeline', () => {
    let timeline = emptyTimeline()
    for (const [eventId, type, payload] of [
      ['run', 'run.started', {}],
      ['turn', 'turn.started', { turn_index: 1 }],
      ['model-start', 'model.started', { model: 'openai/test-model' }],
      ['model-end', 'model.completed', { model: 'test-model' }],
    ] as const) {
      timeline = applyPublicEvent(
        timeline,
        event(eventId, type, payload),
        `epoch:${timeline.appliedEventIds.length + 1}-0`,
      )
    }

    expect(timeline.items).toEqual([])
    expect(timeline.appliedEventIds).toHaveLength(4)
  })
})
