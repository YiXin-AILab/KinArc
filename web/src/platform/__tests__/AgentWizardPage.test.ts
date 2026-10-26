import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { createAppRouter } from '../../router'
import AgentWizardPage from '../AgentWizardPage.vue'

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
vi.mock('../kbClient', () => ({
  defaultKbClient: () => ({
    listBases: vi.fn().mockResolvedValue([]),
    getManifest: vi.fn(),
  }),
}))

const TEMPLATE = {
  config: {
    agent_id: 'banking-agent',
    agent_name: 'banking_agent',
    enabled_skills: [],
    enabled_tools: ['KB_search', 'template_private'],
    output_mode: 'stream',
    llm_model: { type: 'litellm', model: 'claude-sonnet-4' },
  },
  prompt_text: '# 角色',
  status: 'active',
  error: null,
}

const CATALOG = {
  tools: [
    { name: 'KB_search', source: 'builtin', description: null, agent_id: null },
    {
      name: 'template_private',
      source: 'application',
      description: null,
      agent_id: 'banking-agent',
    },
  ],
  middlewares: [],
  context_providers: [],
  outbounds: [{ type: 'stream' }],
  global_skills: [],
}

async function mountPage(path = '/platform/agents/new') {
  const router = createAppRouter(createMemoryHistory())
  await router.push(path)
  await router.isReady()
  const wrapper = mount(AgentWizardPage, { global: { plugins: [router] } })
  await flushPromises()
  return wrapper
}

async function next(wrapper: Awaited<ReturnType<typeof mountPage>>) {
  const button = wrapper
    .findAll('button')
    .find((b) => b.text().includes('下一步') || b.text().includes('创建 Agent'))
  await button!.trigger('click')
  await flushPromises()
}

describe('AgentWizardPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    client.listAgents.mockResolvedValue([
      {
        agent_id: 'banking-agent',
        agent_name: 'banking_agent',
        description: null,
        output_mode: 'stream',
        model: 'claude-sonnet-4',
        tool_count: 1,
        skill_count: 0,
        status: 'active',
        error: null,
      },
    ])
    client.getAgent.mockResolvedValue(TEMPLATE)
    client.catalog.mockResolvedValue(CATALOG)
    client.validateAgent.mockResolvedValue({ errors: [], warnings: [] })
    client.createAgent.mockResolvedValue({ agent_id: 'loan-advisor' })
  })

  it('blocks advancing when agent_id is empty', async () => {
    const wrapper = await mountPage()
    expect(wrapper.text()).toContain('第 1 / 6 步')

    await next(wrapper)

    expect(wrapper.text()).toContain('agent_id 不能为空')
    expect(wrapper.text()).toContain('第 1 / 6 步')
    wrapper.unmount()
  })

  it('walks all six steps and creates the agent', async () => {
    const wrapper = await mountPage()
    await wrapper.get('[aria-label="agent_id"]').setValue('loan-advisor')
    await wrapper.get('[aria-label="agent_name"]').setValue('loan_advisor')

    await next(wrapper) // → Prompt（模板已预填）
    expect(wrapper.text()).toContain('写系统 Prompt')
    await next(wrapper) // → 模型
    await next(wrapper) // → 能力
    await next(wrapper) // → 输出与压缩
    await next(wrapper) // → 确认创建
    expect(wrapper.text()).toContain('确认并创建')
    expect(wrapper.text()).toContain('loan-advisor')

    await next(wrapper) // 创建
    expect(client.validateAgent).toHaveBeenCalled()
    expect(client.createAgent).toHaveBeenCalledWith(
      expect.objectContaining({
        config: expect.objectContaining({ agent_id: 'loan-advisor' }),
        prompt_text: '# 角色',
      }),
    )
    wrapper.unmount()
  })

  it('warns about template private tools on the capabilities step', async () => {
    const wrapper = await mountPage()
    await wrapper.get('[aria-label="agent_id"]').setValue('loan-advisor')
    await wrapper.get('[aria-label="agent_name"]').setValue('loan_advisor')
    await next(wrapper) // → 提示词
    await next(wrapper) // → 模型
    await next(wrapper) // → 能力

    // 新 Agent 只显示 builtin；模板私有工具进入警示条
    const rows = wrapper.findAll('.platform-toolrow')
    expect(rows).toHaveLength(1)
    expect(rows[0]!.text()).toContain('KB_search')
    expect(wrapper.text()).toContain('template_private')
    expect(wrapper.text()).toContain('属于 banking-agent 的私有插件')

    // 点击警示条中的 × 可移除不可解析工具
    const warnTag = wrapper
      .findAll('.platform-chk-warn .platform-captag')
      .find((tag) => tag.text().includes('template_private'))
    await warnTag!.get('i').trigger('click')
    expect(wrapper.text()).not.toContain('不在本 Agent 可用范围')
    wrapper.unmount()
  })

  it('prefills from the ?from= template', async () => {
    const wrapper = await mountPage('/platform/agents/new?from=banking-agent')

    expect(client.getAgent).toHaveBeenCalledWith('banking-agent')
    await wrapper.get('[aria-label="agent_id"]').setValue('copy-agent')
    await wrapper.get('[aria-label="agent_name"]').setValue('copy_agent')
    await next(wrapper) // → Prompt（模板内容已预填，校验可通过）
    await next(wrapper) // → 模型
    expect(wrapper.get<HTMLInputElement>('[aria-label="model"]').element.value).toBe(
      'claude-sonnet-4',
    )
    wrapper.unmount()
  })
})
