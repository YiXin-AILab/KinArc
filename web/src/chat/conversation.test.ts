import { describe, expect, it } from 'vitest'

import type { PublicMessage, StreamTimeline } from '../domain/types'
import { buildConversationEntries, visibleConversationEvents } from './conversation'

const user: PublicMessage = {
  message_id: 'user-1',
  role: 'user',
  origin: 'user',
  content: 'Question',
  created_at: '2026-09-22T10:00:00Z',
  is_error: false,
}

/** 构造最小的持久化消息，并显式提供场景特有字段。 */
function message(
  messageId: string,
  role: PublicMessage['role'],
  content: string,
  extra: Partial<PublicMessage> = {},
): PublicMessage {
  return { ...user, message_id: messageId, role, content, ...extra }
}

function liveTimeline(content: string): StreamTimeline {
  return {
    items: [
      {
        kind: 'block',
        key: 'assistant-1-0',
        messageId: 'assistant-1',
        blockType: 'text',
        content,
        completed: false,
        createdAt: user.created_at,
      },
    ],
    appliedEventIds: [],
  }
}

describe('conversation assembly', () => {
  it('preserves whitespace and stable keys when a live reply becomes history', () => {
    const text = '\n\nAnswer\n  indented\n\n'
    const timeline = liveTimeline(text)
    const live = buildConversationEntries([user], timeline)
    const stored = buildConversationEntries(
      [
        user,
        {
          ...user,
          message_id: 'assistant-1',
          role: 'assistant',
          origin: 'model',
          content: text,
          blocks: [{ type: 'text', text }],
        },
      ],
      { items: [], appliedEventIds: [] },
    )

    expect(live[1]?.key).toBe(stored[1]?.key)
    expect(live[1]).toMatchObject({
      kind: 'agent',
      parts: [{ key: 'assistant-1-0', text, isLive: true }],
    })
    expect(stored[1]).toMatchObject({
      kind: 'agent',
      parts: [{ key: 'assistant-1-0', text, isLive: false }],
    })
  })

  it('does not mutate messages or timeline while enriching a tool result', () => {
    const messages: PublicMessage[] = [
      user,
      message('assistant-1', 'assistant', '', {
        origin: 'model',
        blocks: [
          { type: 'tool_call', tool_call_id: 'call-1', tool_name: 'search' },
          {
            type: 'tool_result',
            tool_call_id: 'call-1',
            tool_name: 'search',
            content: 'Result',
            is_error: false,
          },
        ],
      }),
    ]
    const timeline: StreamTimeline = {
      items: [
        {
          kind: 'tool',
          key: 'call-1',
          name: 'search',
          status: 'completed',
          content: 'Result',
          durationMs: 25,
          createdAt: user.created_at,
        },
      ],
      appliedEventIds: [],
      terminal: { status: 'succeeded' },
    }
    const before = JSON.stringify({ messages, timeline })
    const entries = buildConversationEntries(messages, timeline)
    expect(JSON.stringify({ messages, timeline })).toBe(before)
    expect(entries[1]).toMatchObject({
      parts: [{ kind: 'tool', content: 'Result', status: 'completed', durationMs: 25 }],
    })
  })

  it('preserves whitespace-only live blocks and excludes status events', () => {
    const timeline = liveTimeline('\n\n')
    timeline.items.push({
      kind: 'status',
      key: 'status-1',
      label: 'Running',
      tone: 'active',
      createdAt: user.created_at,
    })
    expect(visibleConversationEvents([], timeline)).toHaveLength(1)
    expect(buildConversationEntries([], timeline)[0]).toMatchObject({
      parts: [{ kind: 'text', text: '\n\n' }],
    })
  })

  it('treats system messages as inbound conversation boundaries', () => {
    const messages: PublicMessage[] = [
      message('user-1', 'user', 'first', {
        origin: 'user',
        created_at: '2026-08-14T10:00:00Z',
      }),
      message('assistant-1', 'assistant', 'first answer', {
        origin: 'model',
        created_at: '2026-08-14T10:00:01Z',
      }),
      message('system-1', 'system', 'business notice', {
        origin: 'workflow.notice',
        created_at: '2026-08-14T10:00:02Z',
      }),
      message('assistant-2', 'assistant', 'second answer', {
        origin: 'model',
        created_at: '2026-08-14T10:00:03Z',
      }),
    ]
    const timeline: StreamTimeline = { items: [], appliedEventIds: [] }
    const entries = buildConversationEntries(messages, timeline)
    expect(entries).toMatchObject([
      { kind: 'user', message: { content: 'first' } },
      { kind: 'agent', parts: [{ kind: 'text', text: 'first answer' }] },
      {
        kind: 'user',
        message: {
          role: 'system',
          origin: 'workflow.notice',
          content: 'business notice',
        },
      },
      { kind: 'agent', parts: [{ kind: 'text', text: 'second answer' }] },
    ])
  })

  it('uses persisted compaction order and removes matching live items', () => {
    const messages: PublicMessage[] = [
      message('user', 'user', '问题', {
        origin: 'user',
        blocks: [{ type: 'text', text: '问题' }],
        created_at: '2026-08-04T10:00:00Z',
      }),
      message('before', 'assistant', '压缩前内容', {
        origin: 'model',
        blocks: [{ type: 'text', text: '压缩前内容' }],
        created_at: '2026-08-04T10:00:01Z',
      }),
      message('compaction-message', 'user', '', {
        origin: 'compactor',
        blocks: [{ type: 'compaction' }],
        created_at: '2026-08-04T10:00:02Z',
      }),
      message('middle', 'assistant', '两次压缩之间', {
        origin: 'model',
        blocks: [{ type: 'text', text: '两次压缩之间' }],
        created_at: '2026-08-04T10:00:03Z',
      }),
      message('compaction-message-2', 'user', '', {
        origin: 'compactor',
        blocks: [{ type: 'compaction' }],
        created_at: '2026-08-04T10:00:04Z',
      }),
      message('answer', 'assistant', '最终回答', {
        origin: 'model',
        blocks: [{ type: 'text', text: '最终回答' }],
        created_at: '2026-08-04T10:00:05Z',
      }),
    ]
    const timeline: StreamTimeline = {
      items: [
        {
          kind: 'compaction',
          key: 'compact-start',
          messageId: 'compaction-message',
          status: 'completed',
          beforeTokens: 10000,
          afterTokens: 6000,
          durationMs: 860,
          createdAt: '2026-08-04T10:00:02Z',
        },
        {
          kind: 'compaction',
          key: 'compact-start-2',
          messageId: 'compaction-message-2',
          status: 'completed',
          createdAt: '2026-08-04T10:00:04Z',
        },
      ],
      appliedEventIds: ['compact-start', 'compact-end'],
    }
    const entries = buildConversationEntries(messages, timeline)
    expect(entries).toHaveLength(2)
    expect(entries[1]).toMatchObject({
      kind: 'agent',
      parts: [
        { kind: 'text', text: '压缩前内容' },
        { kind: 'compaction', beforeTokens: 10000, afterTokens: 6000, durationMs: 860 },
        { kind: 'text', text: '两次压缩之间' },
        { kind: 'compaction' },
        { kind: 'text', text: '最终回答' },
      ],
    })
    expect(visibleConversationEvents(messages, timeline)).toEqual([])
  })

  it('assembles persisted thinking, tool name, result and final answer in message order', () => {
    const messages: PublicMessage[] = [
      message('assistant-tool-call', 'assistant', '', {
        origin: 'model',
        blocks: [
          { type: 'thinking', thinking: '先查找官方资料' },
          {
            type: 'tool_call',
            tool_call_id: 'call-1',
            tool_name: 'web_search',
          },
        ],
        created_at: '2026-08-04T10:00:00Z',
      }),
      message('tool-result', 'tool', '搜索结果', {
        origin: 'tool',
        blocks: [
          {
            type: 'tool_result',
            tool_call_id: 'call-1',
            tool_name: 'web_search',
            content: '搜索结果',
            is_error: false,
          },
        ],
        created_at: '2026-08-04T10:00:01Z',
      }),
      message('assistant-final', 'assistant', '最终答案', {
        origin: 'model',
        blocks: [{ type: 'text', text: '最终答案' }],
        created_at: '2026-08-04T10:00:02Z',
      }),
    ]
    const timeline: StreamTimeline = {
      items: [
        {
          kind: 'block',
          key: 'stale-thinking',
          blockType: 'thinking',
          content: '临时思考不应重复',
          completed: true,
          createdAt: '2026-08-04T10:00:00Z',
        },
        {
          kind: 'status',
          key: 'model-started',
          label: '模型生成中',
          detail: 'openai/deepseek-v4-flash',
          tone: 'active',
          createdAt: '2026-08-04T10:00:00Z',
        },
      ],
      appliedEventIds: ['terminal'],
      terminal: { status: 'succeeded' },
    }
    const entries = buildConversationEntries(messages, timeline)
    expect(entries).toHaveLength(1)
    expect(entries[0]).toMatchObject({
      kind: 'agent',
      parts: [
        { kind: 'thinking', content: '先查找官方资料' },
        { kind: 'tool', name: 'web_search', content: '搜索结果' },
        { kind: 'text', text: '最终答案' },
      ],
    })
    expect(visibleConversationEvents(messages, timeline)).toEqual([])
  })

  it('pairs repeated tool names by tool call id', () => {
    const messages: PublicMessage[] = [
      message('assistant-calls', 'assistant', '', {
        origin: 'model',
        blocks: [
          { type: 'tool_call', tool_call_id: 'first', tool_name: 'search' },
          { type: 'tool_call', tool_call_id: 'second', tool_name: 'search' },
        ],
        created_at: '2026-08-04T10:00:00Z',
      }),
      message('second-result', 'tool', 'second output', {
        origin: 'tool',
        blocks: [
          {
            type: 'tool_result',
            tool_call_id: 'second',
            tool_name: 'search',
            content: 'second output',
            is_error: false,
          },
        ],
        created_at: '2026-08-04T10:00:01Z',
      }),
    ]
    const timeline: StreamTimeline = { items: [], appliedEventIds: [] }
    const entries = buildConversationEntries(messages, timeline)
    expect(entries).toHaveLength(1)
    expect(entries[0]).toMatchObject({
      kind: 'agent',
      parts: [
        { kind: 'tool', toolCallId: 'first', name: 'search' },
        {
          kind: 'tool',
          toolCallId: 'second',
          name: 'search',
          content: 'second output',
        },
      ],
    })
    if (entries[0]?.kind !== 'agent') throw new Error('Expected agent entry')
    expect(entries[0].parts[0]).not.toHaveProperty('content', 'second output')
  })

  it('groups live thinking, tool start/result and text into one agent entry', () => {
    const messages: PublicMessage[] = [
      message('user', 'user', '搜索 Vue', {
        origin: 'user',
        created_at: '2026-08-04T10:00:00Z',
      }),
    ]
    const timeline: StreamTimeline = {
      items: [
        {
          kind: 'block',
          key: 'thinking',
          blockType: 'thinking',
          content: '先分析',
          completed: true,
          createdAt: '2026-08-04T10:00:01Z',
        },
        {
          kind: 'tool',
          key: 'call-1',
          name: 'web_search',
          status: 'completed',
          content: '搜索结果',
          durationMs: 42,
          createdAt: '2026-08-04T10:00:02Z',
        },
        {
          kind: 'block',
          key: 'answer',
          blockType: 'text',
          content: '最终回答',
          completed: true,
          createdAt: '2026-08-04T10:00:03Z',
        },
        {
          kind: 'status',
          key: 'turn',
          label: '新一轮推理',
          detail: '第 1 轮',
          tone: 'active',
          createdAt: '2026-08-04T10:00:01Z',
        },
      ],
      appliedEventIds: [],
    }
    const entries = buildConversationEntries(messages, timeline)
    expect(entries).toHaveLength(2)
    expect(entries[1]).toMatchObject({
      kind: 'agent',
      parts: [
        { kind: 'thinking', content: '先分析' },
        {
          kind: 'tool',
          name: 'web_search',
          status: 'completed',
          content: '搜索结果',
          durationMs: 42,
        },
        { kind: 'text', text: '最终回答' },
      ],
    })
  })

  it('keeps pre-steer live work before the started steer message', () => {
    const messages: PublicMessage[] = [
      message('user-1', 'user', '问题一', {
        origin: 'user',
        created_at: '2026-08-04T10:00:00Z',
      }),
      message('user-2', 'user', '问题二', {
        origin: 'user',
        created_at: '2026-08-04T10:00:01Z',
      }),
    ]
    const timeline: StreamTimeline = {
      items: [
        {
          kind: 'message-boundary',
          key: 'user-1',
          messageId: 'user-1',
          createdAt: '2026-08-04T10:00:01Z',
        },
        {
          kind: 'block',
          key: 'thinking-1',
          blockType: 'thinking',
          content: '问题一思考',
          completed: true,
          createdAt: '2026-08-04T10:00:02Z',
        },
        {
          kind: 'tool',
          key: 'call-1',
          name: 'search',
          status: 'completed',
          content: '问题一工具结果',
          createdAt: '2026-08-04T10:00:03Z',
        },
        {
          kind: 'message-boundary',
          key: 'user-2',
          messageId: 'user-2',
          createdAt: '2026-08-04T10:00:04Z',
        },
        {
          kind: 'block',
          key: 'answer-2',
          blockType: 'text',
          content: '问题二后的回答',
          completed: false,
          createdAt: '2026-08-04T10:00:05Z',
        },
      ],
      appliedEventIds: [],
    }
    const entries = buildConversationEntries(messages, timeline)
    expect(entries).toMatchObject([
      { kind: 'user', message: { message_id: 'user-1' } },
      {
        kind: 'agent',
        parts: [
          { kind: 'thinking', content: '问题一思考' },
          { kind: 'tool', name: 'search', content: '问题一工具结果' },
        ],
      },
      { kind: 'user', message: { message_id: 'user-2' } },
      { kind: 'agent', parts: [{ kind: 'text', text: '问题二后的回答' }] },
    ])
  })
})
