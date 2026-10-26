import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { StreamTimeline } from '../domain/types'
import MessageTimeline from './MessageTimeline.vue'

enableAutoUnmount(afterEach)

describe('MessageTimeline', () => {
  it('updates context compaction from running to completed', async () => {
    const wrapper = mount(MessageTimeline, {
      props: {
        messages: [],
        timeline: {
          items: [
            {
              kind: 'compaction',
              key: 'compact-running',
              status: 'running',
              createdAt: '2026-08-04T10:00:00Z',
            },
          ],
          appliedEventIds: ['compact-start'],
        },
        isBusy: true,
      },
    })

    const compaction = wrapper.get('.compaction-event')
    expect(compaction.get('.event-status-dot').attributes('aria-label')).toBe('运行中')
    await wrapper.setProps({
      timeline: {
        items: [
          {
            kind: 'compaction',
            key: 'compact-running',
            status: 'completed',
            action: 'summarized',
            beforeTokens: 9000,
            afterTokens: 5000,
            durationMs: 1200,
            createdAt: '2026-08-04T10:00:00Z',
          },
        ],
        appliedEventIds: ['compact-start', 'compact-end'],
      },
      isBusy: false,
    })
    expect(compaction.get('.event-status-dot').attributes('aria-label')).toBe('已完成')
    expect(compaction.text()).toContain('44%')
    expect(compaction.text()).toContain('1.2s')
  })

  it('renders user media before trailing text through authenticated reading', async () => {
    const readAsset = vi.fn().mockRejectedValue(new Error('unavailable'))
    const wrapper = mount(MessageTimeline, {
      props: {
        messages: [
          {
            message_id: 'user-media',
            role: 'user',
            origin: 'user',
            content: '描述图片',
            blocks: [
              {
                type: 'image',
                source_type: 'asset',
                media_type: 'image/png',
                filename: 'image.png',
                asset_id: 'asset-1',
              },
              { type: 'text', text: '描述图片' },
            ],
            created_at: '2026-08-04T10:00:00Z',
            is_error: false,
          },
        ],
        timeline: { items: [], appliedEventIds: [] },
        isBusy: false,
        readAsset,
      },
    })
    await flushPromises()

    expect(
      wrapper
        .findAll('[data-user-block]')
        .map((item) => item.attributes('data-user-block')),
    ).toEqual(['image', 'text'])
    expect(readAsset).toHaveBeenCalledWith('asset-1', expect.any(AbortSignal))
  })

  it('renders authoritative messages and distinct thinking/tool/metric views', () => {
    const timeline: StreamTimeline = {
      cursor: 'epoch:4-0',
      appliedEventIds: ['one'],
      items: [
        {
          kind: 'block',
          key: 'thinking',
          blockType: 'thinking',
          content: '先分析问题',
          completed: true,
          createdAt: '2026-08-04T10:00:00Z',
        },
        {
          kind: 'tool',
          key: 'call',
          name: 'web_search',
          status: 'completed',
          durationMs: 86,
          createdAt: '2026-08-04T10:00:01Z',
        },
      ],
      terminal: {
        status: 'succeeded',
        usage: { input_tokens: 10, output_tokens: 5, total_tokens: 15 },
        timing: { elapsed_ms: 1200, active_ms: 900 },
      },
    }
    const wrapper = mount(MessageTimeline, {
      props: {
        messages: [
          {
            message_id: 'user',
            role: 'user',
            origin: 'user',
            content: '查一下 Vue',
            created_at: '2026-08-04T10:00:00Z',
            is_error: false,
          },
          {
            message_id: 'answer',
            role: 'assistant',
            origin: 'model',
            content: '最终答案',
            blocks: [
              { type: 'thinking', thinking: '先分析问题' },
              { type: 'tool_call', tool_call_id: 'call', tool_name: 'web_search' },
              {
                type: 'tool_result',
                tool_call_id: 'call',
                tool_name: 'web_search',
                content: '搜索结果',
                is_error: false,
              },
              { type: 'text', text: '最终答案' },
            ],
            created_at: '2026-08-04T10:00:02Z',
            is_error: false,
          },
        ],
        timeline,
        isBusy: false,
      },
    })

    expect(wrapper.text()).toContain('查一下 Vue')
    expect(wrapper.get('.thinking-event').text()).toContain('先分析问题')
    expect(wrapper.text()).toContain('web_search')
    expect(wrapper.get('.tool-event summary').text()).toContain('86ms')
    expect(wrapper.findAll('.message-agent')).toHaveLength(1)
    expect(wrapper.get('.message-agent').text()).toContain('最终答案')
    expect(wrapper.get('.tool-event').text()).toContain('搜索结果')
    expect(wrapper.text()).toContain('15 tokens')
    expect(wrapper.text()).toContain('1.2s')
  })

  it('shows run failure details instead of a generic terminal summary', () => {
    const wrapper = mount(MessageTimeline, {
      props: {
        messages: [],
        timeline: {
          items: [],
          appliedEventIds: ['failed'],
          terminal: {
            status: 'failed',
            error: {
              code: 'context_compaction_failed',
              message: 'Context compaction failed',
              retryable: false,
            },
            usage: { input_tokens: 10, output_tokens: 0, total_tokens: 10 },
            timing: { elapsed_ms: 1200, active_ms: 900 },
          },
        },
        isBusy: false,
      },
    })

    const summary = wrapper.get('[data-run-summary]')
    expect(summary.attributes('role')).toBe('alert')
    expect(summary.classes()).toContain('run-summary-failed')
    expect(summary.text()).toContain('Context compaction failed')
    expect(summary.text()).toContain('context_compaction_failed')
    expect(summary.text()).toContain('10 tokens')
  })

  it('shows the authoritative cancellation code and message', () => {
    const wrapper = mount(MessageTimeline, {
      props: {
        messages: [],
        timeline: {
          items: [],
          appliedEventIds: ['canceled'],
          terminal: {
            status: 'canceled',
            error: {
              code: 'run_canceled',
              message: 'run 已被用户取消',
              retryable: false,
            },
            usage: { input_tokens: 3, output_tokens: 1, total_tokens: 4 },
            timing: { elapsed_ms: 20, active_ms: 18 },
          },
        },
        isBusy: false,
      },
    })

    const summary = wrapper.get('[data-run-summary]')
    expect(summary.classes()).toContain('run-summary-canceled')
    expect(summary.text()).toContain('run 已被用户取消')
    expect(summary.text()).toContain('run_canceled')
  })

  it('does not attach a previous run summary to a newer user message', () => {
    const wrapper = mount(MessageTimeline, {
      props: {
        messages: [
          {
            message_id: 'assistant-1',
            role: 'assistant',
            origin: 'model',
            content: '上一轮回答',
            created_at: '2026-08-04T10:00:01Z',
            is_error: false,
          },
          {
            message_id: 'user-2',
            role: 'user',
            origin: 'user',
            content: '下一轮问题',
            created_at: '2026-08-04T10:00:03Z',
            is_error: false,
          },
        ],
        timeline: {
          items: [],
          appliedEventIds: ['run-1-terminal'],
          terminal: {
            status: 'succeeded',
            usage: { input_tokens: 10, output_tokens: 5, total_tokens: 15 },
            timing: {
              started_at: '2026-08-04T10:00:00Z',
              ended_at: '2026-08-04T10:00:02Z',
              elapsed_ms: 2000,
              active_ms: 1800,
            },
          },
        },
        isBusy: true,
      },
    })

    expect(wrapper.text()).toContain('下一轮问题')
    expect(wrapper.find('[data-run-summary]').exists()).toBe(false)
  })

  it('keeps long authoritative tool output collapsed by default', () => {
    const wrapper = mount(MessageTimeline, {
      props: {
        messages: [
          {
            message_id: 'tool',
            role: 'tool',
            origin: 'tool',
            content: 'very long raw tool output',
            created_at: '2026-08-04T10:00:00Z',
            is_error: false,
          },
        ],
        timeline: { items: [], appliedEventIds: [] },
        isBusy: false,
      },
    })

    expect(wrapper.get('details').attributes('open')).toBeUndefined()
    expect(wrapper.get('.event-status-dot').attributes('aria-label')).toBe('已完成')
    expect(wrapper.get('details').text()).toContain('very long raw tool output')
  })

  it('shows a completed tool result even when result content is hidden', () => {
    const wrapper = mount(MessageTimeline, {
      props: {
        messages: [
          {
            message_id: 'assistant-call',
            role: 'assistant',
            origin: 'model',
            content: '',
            blocks: [
              { type: 'tool_call', tool_call_id: 'call-1', tool_name: 'search' },
            ],
            created_at: '2026-08-04T10:00:00Z',
            is_error: false,
          },
          {
            message_id: 'tool-result',
            role: 'tool',
            origin: 'tool',
            content: '',
            blocks: [
              {
                type: 'tool_result',
                tool_call_id: 'call-1',
                tool_name: 'search',
                is_error: false,
              },
            ],
            created_at: '2026-08-04T10:00:01Z',
            is_error: false,
          },
        ],
        timeline: { items: [], appliedEventIds: [] },
        isBusy: false,
      },
    })

    expect(wrapper.get('.tool-event .event-status-dot').attributes('aria-label')).toBe(
      '已完成',
    )
    expect(wrapper.get('.tool-event').find('pre').exists()).toBe(false)
  })

  it('renders accessible status labels for completed, running and failed events', () => {
    const wrapper = mount(MessageTimeline, {
      props: {
        messages: [
          {
            message_id: 'user',
            role: 'user',
            origin: 'user',
            content: '执行任务',
            created_at: '2026-08-04T10:00:00Z',
            is_error: false,
          },
        ],
        timeline: {
          items: [
            {
              kind: 'block',
              key: 'assistant-0',
              blockType: 'thinking',
              content: '分析问题',
              completed: true,
              durationMs: 1200,
              createdAt: '2026-08-04T10:00:01Z',
            },
            {
              kind: 'tool',
              key: 'running-call',
              name: 'web_search',
              status: 'running',
              durationMs: 860,
              createdAt: '2026-08-04T10:00:02Z',
            },
            {
              kind: 'tool',
              key: 'failed-call',
              name: 'read_file',
              status: 'failed',
              durationMs: 42,
              createdAt: '2026-08-04T10:00:03Z',
            },
          ],
          appliedEventIds: [],
        },
        isBusy: true,
      },
    })

    const thinkingSummary = wrapper.get('.thinking-event summary')
    const runningSummary = wrapper.get('.tool-event[data-status="running"] summary')
    const failedSummary = wrapper.get('.tool-event[data-status="failed"] summary')
    expect(thinkingSummary.text()).toContain('1.2s')
    expect(runningSummary.text()).toContain('web_search')
    expect(runningSummary.text()).toContain('860ms')
    expect(failedSummary.text()).toContain('read_file')
    expect(failedSummary.text()).toContain('42ms')
    expect(thinkingSummary.get('.event-status-dot').attributes('aria-label')).toBe(
      '已完成',
    )
    expect(runningSummary.get('.event-status-dot').attributes('aria-label')).toBe(
      '运行中',
    )
    expect(failedSummary.get('.event-status-dot').attributes('aria-label')).toBe('失败')
  })

  it('reuses the agent reply element when live output becomes authoritative', async () => {
    const userMessage = {
      message_id: 'user-1',
      role: 'user' as const,
      origin: 'user',
      content: '介绍一下 Vue',
      created_at: '2026-08-04T10:00:00Z',
      is_error: false,
    }
    const wrapper = mount(MessageTimeline, {
      props: {
        messages: [userMessage],
        timeline: {
          items: [
            {
              kind: 'block',
              key: 'assistant-1-0',
              messageId: 'assistant-1',
              blockType: 'text',
              content: 'Vue 是',
              completed: false,
              createdAt: '2026-08-04T10:00:01Z',
            },
          ],
          appliedEventIds: ['answer-delta'],
          cursor: 'epoch:1-0',
        },
        isBusy: true,
      },
    })
    const liveReply = wrapper.get('.message-agent').element
    const liveTextPart = wrapper.get('.message-agent .streaming-answer').element

    await wrapper.setProps({
      messages: [
        userMessage,
        {
          message_id: 'assistant-1',
          role: 'assistant',
          origin: 'model',
          content: 'Vue 是一个渐进式框架',
          blocks: [{ type: 'text', text: 'Vue 是一个渐进式框架' }],
          created_at: '2026-08-04T10:00:01Z',
          is_error: false,
        },
      ],
      timeline: {
        items: [
          {
            kind: 'block',
            key: 'assistant-1-0',
            messageId: 'assistant-1',
            blockType: 'text',
            content: 'Vue 是一个渐进式框架',
            completed: true,
            createdAt: '2026-08-04T10:00:01Z',
          },
        ],
        appliedEventIds: ['answer-delta', 'answer-completed', 'run-completed'],
        cursor: 'epoch:3-0',
        terminal: { status: 'succeeded' },
      },
      isBusy: false,
    })

    expect(wrapper.get('.message-agent').element).toBe(liveReply)
    expect(wrapper.get('.message-agent [data-history-block]').element).toBe(
      liveTextPart,
    )
    expect(wrapper.get('.message-agent').text()).toContain('Vue 是一个渐进式框架')
  })

  it.each([
    { position: 'reading history', scrollTop: 120, expected: 120 },
    { position: 'near the bottom', scrollTop: 390, expected: 500 },
  ])('updates scroll while $position', async ({ scrollTop, expected }) => {
    const wrapper = mount(MessageTimeline, {
      props: {
        messages: [],
        timeline: { items: [], appliedEventIds: [] },
        isBusy: true,
      },
    })
    const viewport = wrapper.get('.timeline-scroll')
    Object.defineProperties(viewport.element, {
      clientHeight: { configurable: true, value: 100 },
      scrollHeight: { configurable: true, value: 500 },
    })
    viewport.element.scrollTop = scrollTop
    await viewport.trigger('scroll')

    await wrapper.setProps({
      timeline: {
        items: [
          {
            kind: 'block',
            key: 'answer',
            messageId: 'assistant-1',
            blockType: 'text',
            content: '新增输出',
            completed: false,
            createdAt: '2026-08-04T10:00:01Z',
          },
        ],
        appliedEventIds: ['answer'],
        cursor: 'epoch:1-0',
      },
    })
    await nextTick()

    expect(viewport.element.scrollTop).toBe(expected)
  })
})
