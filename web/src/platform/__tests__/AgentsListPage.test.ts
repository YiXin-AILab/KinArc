import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { createAppRouter } from '../../router'
import AgentsListPage from '../AgentsListPage.vue'

const { client } = vi.hoisted(() => ({
  client: {
    listAgents: vi.fn(),
    createAgent: vi.fn(),
    getAgent: vi.fn(),
    updateAgent: vi.fn(),
    deleteAgent: vi.fn(),
    validateAgent: vi.fn(),
    catalog: vi.fn(),
    listSkills: vi.fn(),
    createSkill: vi.fn(),
    getSkill: vi.fn(),
    updateSkill: vi.fn(),
    deleteSkill: vi.fn(),
    listSkillResources: vi.fn(),
    putSkillResource: vi.fn(),
    getSkillResource: vi.fn(),
    deleteSkillResource: vi.fn(),
    skillReferences: vi.fn(),
  },
}))

vi.mock('../client', () => ({ defaultPlatformClient: () => client }))

function agent(partial: Record<string, unknown>) {
  return {
    agent_id: 'a',
    agent_name: 'A',
    description: null,
    output_mode: 'stream',
    model: 'claude-sonnet-4',
    tool_count: 1,
    skill_count: 0,
    status: 'active',
    error: null,
    ...partial,
  }
}

async function mountPage() {
  const router = createAppRouter(createMemoryHistory())
  await router.push('/platform/agents')
  await router.isReady()
  const wrapper = mount(AgentsListPage, { global: { plugins: [router] } })
  await flushPromises()
  return wrapper
}

describe('AgentsListPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders stats and agent cards with status badges', async () => {
    client.listAgents.mockResolvedValue([
      agent({ agent_id: 'banking-agent', agent_name: 'banking_agent' }),
      agent({ agent_id: 'mm', agent_name: 'mm_agent', status: 'pending' }),
      agent({
        agent_id: 'broken',
        agent_name: 'broken_agent',
        status: 'invalid',
        error: 'llm_model 校验失败',
      }),
    ])
    const wrapper = await mountPage()

    expect(wrapper.text()).toContain('Agent 总数')
    const statValues = wrapper.findAll('.platform-stat-value')
    expect(statValues.map((node) => node.text())).toEqual(['3', '1', '1', '1'])
    expect(wrapper.text()).toContain('生效中')
    expect(wrapper.text()).toContain('待重启生效')
    expect(wrapper.text()).toContain('配置无效')
    expect(wrapper.text()).toContain('llm_model 校验失败')
    wrapper.unmount()
  })

  it('asks for confirmation and deletes via the client', async () => {
    client.listAgents.mockResolvedValue([
      agent({ agent_id: 'broken', status: 'invalid' }),
    ])
    client.deleteAgent.mockResolvedValue(undefined)
    const wrapper = await mountPage()

    await wrapper.get('.platform-btn-dngr').trigger('click')
    await flushPromises()
    const modal = document.querySelector('.platform-modal')
    expect(modal?.textContent).toContain('删除 Agent')
    const buttons = [...document.querySelectorAll('.platform-modal button')]
    const confirm = buttons.find((b) =>
      b.textContent?.includes('确认删除'),
    ) as HTMLButtonElement
    confirm.click()
    await flushPromises()

    expect(client.deleteAgent).toHaveBeenCalledWith('broken')
    wrapper.unmount()
  })

  it('shows error state and retries', async () => {
    client.listAgents.mockRejectedValueOnce(new Error('boom')).mockResolvedValueOnce([])
    const wrapper = await mountPage()
    expect(wrapper.get('[role="alert"]').text()).toContain('boom')

    await wrapper.get('.platform-state .platform-btn').trigger('click')
    await flushPromises()
    expect(client.listAgents).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })
})
