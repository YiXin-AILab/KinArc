import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'

import ChatContextPanel from './ChatContextPanel.vue'
import type { AgentSummary } from '../api/platformClient'
import type { AgentDefinition } from '../config'
import type {
  SessionSnapshot,
  SessionStatus,
  StreamTimeline,
  TimelineItem,
} from '../domain/types'

const agent: AgentDefinition = {
  id: 'banking-agent',
  name: 'banking_agent',
  description: '银行助手',
  outputMode: 'stream',
}

function snapshot(partial: Partial<SessionSnapshot> = {}): SessionSnapshot {
  return { session_id: '9f3a42bc-def0', status: 'idle', ...partial }
}

function timeline(items: TimelineItem[] = []): StreamTimeline {
  return { items, appliedEventIds: [] }
}

function item(partial: Record<string, unknown>): TimelineItem {
  return { key: 'k', createdAt: '2026-09-29T10:00:00Z', ...partial } as TimelineItem
}

function meta(partial: Partial<AgentSummary> = {}): AgentSummary {
  return {
    agent_id: 'banking_agent',
    agent_name: 'banking_agent',
    description: null,
    output_mode: 'stream',
    model: 'claude-sonnet-4-5',
    tool_count: 14,
    skill_count: 3,
    status: 'active',
    error: null,
    ...partial,
  }
}

function mountPanel(options: {
  snap?: SessionSnapshot | null
  items?: TimelineItem[]
  loadAgentMeta?: () => Promise<AgentSummary[]>
}) {
  return mount(ChatContextPanel, {
    props: {
      snapshot: options.snap ?? snapshot(),
      timeline: timeline(options.items ?? []),
      agent,
      loadAgentMeta: options.loadAgentMeta ?? vi.fn().mockResolvedValue([meta()]),
    },
  })
}

describe('ChatContextPanel', () => {
  it('maps snapshot status to Chinese badges', () => {
    const cases: [SessionStatus, string, string][] = [
      ['idle', '空闲', 'ctx-b-mut'],
      ['running', '运行中', 'ctx-b-cyan'],
      ['pending', '待确认', 'ctx-b-amber'],
      ['succeeded', '已完成', 'ctx-b-green'],
      ['failed', '失败', 'ctx-b-red'],
      ['canceled', '已取消', 'ctx-b-mut'],
    ]
    for (const [status, label, cls] of cases) {
      const wrapper = mountPanel({ snap: snapshot({ status }) })
      const badge = wrapper.get('[data-testid="ctx-status"]')
      expect(badge.text()).toContain(label)
      expect(badge.classes()).toContain(cls)
      wrapper.unmount()
    }
  })

  it('renders session id, tokens and elapsed from the snapshot', () => {
    const wrapper = mountPanel({
      snap: snapshot({
        status: 'running',
        usage: { input_tokens: 8000, output_tokens: 4847, total_tokens: 12847 },
        timing: { elapsed_ms: 46000, active_ms: 40000 },
      }),
    })
    const card = wrapper.get('[data-testid="ctx-run-card"]')
    expect(card.text()).toContain('9f3a42bc…')
    expect(card.text()).toContain('banking_agent')
    expect(card.text()).toContain('12,847')
    expect(card.text()).toContain('46.0s')
    wrapper.unmount()
  })

  it('degrades to dashes when usage and timing are absent', () => {
    const wrapper = mountPanel({ snap: snapshot({ usage: null, timing: null }) })
    const card = wrapper.get('[data-testid="ctx-run-card"]')
    expect(card.text()).toContain('tokens')
    expect(card.text()).toContain('—')
    wrapper.unmount()
  })

  it('derives execution steps from the timeline with running/completed/failed states', () => {
    const wrapper = mountPanel({
      snap: snapshot({ status: 'running' }),
      items: [
        item({ kind: 'message-boundary', key: 'b1', messageId: 'm1' }),
        item({
          kind: 'block',
          key: 'b2',
          blockType: 'text',
          content: '',
          completed: true,
        }),
        item({
          kind: 'tool',
          key: 't1',
          name: 'get_account_balance',
          status: 'completed',
        }),
        item({ kind: 'tool', key: 't2', name: 'freeze_card', status: 'running' }),
        item({ kind: 'compaction', key: 'c1', status: 'completed' }),
        item({ kind: 'tool', key: 't3', name: 'wire_transfer', status: 'failed' }),
        item({ kind: 'status', key: 's1', label: '忽略状态项', tone: 'neutral' }),
      ],
    })
    const steps = wrapper.get('[data-testid="ctx-steps"]')
    expect(steps.text()).toContain('接收输入')
    expect(steps.text()).toContain('LLM 生成')
    expect(steps.text()).toContain('get_account_balance')
    expect(steps.text()).toContain('freeze_card')
    expect(steps.text()).toContain('上下文压缩')
    expect(steps.text()).toContain('wire_transfer')
    expect(steps.text()).not.toContain('忽略状态项')
    const nodes = wrapper.findAll('.ctx-step')
    expect(nodes.map((node) => node.classes())).toEqual([
      expect.arrayContaining(['done']),
      expect.arrayContaining(['done']),
      expect.arrayContaining(['done']),
      expect.arrayContaining(['run']),
      expect.arrayContaining(['done']),
      expect.arrayContaining(['fail']),
    ])
    wrapper.unmount()
  })

  it('keeps only the latest 8 steps', () => {
    const items = Array.from({ length: 12 }, (_, index) =>
      item({
        kind: 'tool',
        key: `t${index}`,
        name: `tool_${index}`,
        status: 'completed',
      }),
    )
    const wrapper = mountPanel({ items })
    const steps = wrapper.findAll('.ctx-step')
    expect(steps).toHaveLength(8)
    expect(steps[0]!.text()).toContain('tool_4')
    wrapper.unmount()
  })

  it('shows an empty hint when no events exist yet', () => {
    const wrapper = mountPanel({})
    expect(wrapper.get('[data-testid="ctx-steps"]').text()).toContain('等待运行事件')
    wrapper.unmount()
  })

  it('matches agent metadata by underscore-normalized id and shows model/tools/skills', async () => {
    const loadAgentMeta = vi.fn().mockResolvedValue([meta()])
    const wrapper = mountPanel({ loadAgentMeta })
    await flushPromises()

    expect(loadAgentMeta).toHaveBeenCalledTimes(1)
    const card = wrapper.get('[data-testid="ctx-agent-card"]')
    expect(card.text()).toContain('banking_agent')
    expect(card.text()).toContain('claude-sonnet-4-5')
    expect(card.text()).toContain('14 启用')
    expect(card.text()).toContain('3 挂载')
    wrapper.unmount()
  })

  it('silently falls back to the runtime agent definition when meta fetch fails', async () => {
    const loadAgentMeta = vi.fn().mockRejectedValue(new Error('boom'))
    const wrapper = mountPanel({ loadAgentMeta })
    await flushPromises()

    const card = wrapper.get('[data-testid="ctx-agent-card"]')
    expect(card.text()).toContain('banking_agent')
    expect(card.text()).toContain('银行助手')
    expect(card.text()).not.toContain('启用')
    wrapper.unmount()
  })
})
