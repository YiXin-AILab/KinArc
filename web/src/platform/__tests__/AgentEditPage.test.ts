import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { createAppRouter } from '../../router'
import AgentEditPage from '../AgentEditPage.vue'

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
  defaultKbClient: () => kbClient,
}))

const { kbClient } = vi.hoisted(() => ({
  kbClient: {
    listBases: vi.fn(),
    getManifest: vi.fn(),
  },
}))

const KB_PRICING = {
  name: 'pricing',
  scope: 'global',
  description: '价格库',
  doc_count: 22,
  strategy: 'bm25',
  top_k: 5,
  has_rerank: false,
  has_gate: true,
  referenced_by: [],
  valid: true,
  error: null,
}

const DETAIL = {
  config: {
    agent_id: 'banking-agent',
    agent_name: 'banking_agent',
    prompt: 'prompt/system.md',
    enabled_skills: ['kb-writing-standard'],
    enabled_tools: ['KB_search'],
    output_mode: 'stream',
    llm_model: {
      type: 'litellm',
      model: 'claude-sonnet-4',
      api_key: '${TEST_KEY}',
      temperature: 0,
      max_output_tokens: 8192,
      context_window: 128000,
    },
  },
  prompt_text: '# 角色\n你是银行客服助手',
  status: 'active',
  error: null,
}

const CATALOG = {
  tools: [
    { name: 'KB_search', source: 'builtin', description: null, agent_id: null },
    {
      name: 'log_verification',
      source: 'application',
      description: null,
      agent_id: 'banking-agent',
    },
    {
      name: 'other_private_tool',
      source: 'application',
      description: null,
      agent_id: 'other-agent',
    },
  ],
  middlewares: [
    { name: 'system-prompt', source: 'builtin', description: null, agent_id: null },
  ],
  context_providers: [],
  outbounds: [{ type: 'stream' }],
  global_skills: [{ name: 'kb-writing-standard', description: '写作规范' }],
}

async function mountPage() {
  const router = createAppRouter(createMemoryHistory())
  await router.push('/platform/agents/banking-agent/edit')
  await router.isReady()
  const wrapper = mount(AgentEditPage, { global: { plugins: [router] } })
  await flushPromises()
  return wrapper
}

describe('AgentEditPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    client.getAgent.mockResolvedValue(DETAIL)
    client.catalog.mockResolvedValue(CATALOG)
    client.listSkills.mockResolvedValue({ skills: [], global_skills: null })
    client.validateAgent.mockResolvedValue({ errors: [], warnings: [] })
    client.updateAgent.mockResolvedValue({
      agent_id: 'banking-agent',
      status: 'pending',
    })
    kbClient.listBases.mockResolvedValue([KB_PRICING])
  })

  it('loads the config and renders the YAML preview', async () => {
    const wrapper = await mountPage()

    expect(wrapper.text()).toContain('banking_agent')
    expect(wrapper.text()).toContain('生效中')
    const yaml = wrapper.get('.platform-yaml')
    expect(yaml.text()).toContain('agent_id: banking-agent')
    expect(yaml.text()).toContain('model: claude-sonnet-4')
    expect(client.getAgent).toHaveBeenCalledWith('banking-agent')
    wrapper.unmount()
  })

  it('blocks saving when validation reports errors', async () => {
    client.validateAgent.mockResolvedValue({
      errors: [{ field: 'llm_model.context_window', message: '上下文窗口过小' }],
      warnings: [],
    })
    const wrapper = await mountPage()

    const saveButton = wrapper.findAll('button').find((b) => b.text().includes('保存'))
    await saveButton!.trigger('click')
    await flushPromises()

    expect(client.validateAgent).toHaveBeenCalled()
    expect(client.updateAgent).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('上下文窗口过小')
    wrapper.unmount()
  })

  it('filters tool options to builtin + own private and warns about cross-agent tools', async () => {
    client.getAgent.mockResolvedValue({
      ...DETAIL,
      config: {
        ...DETAIL.config,
        enabled_tools: ['KB_search', 'other_private_tool'],
      },
    })
    const wrapper = await mountPage()

    const rows = wrapper.findAll('.platform-toolrow')
    const rowNames = rows.map((row) => row.text())
    // builtin + 本 Agent 私有可见；其他 Agent 私有不出现在勾选列表
    expect(rowNames.some((text) => text.includes('KB_search'))).toBe(true)
    expect(rowNames.some((text) => text.includes('log_verification'))).toBe(true)
    expect(rowNames.some((text) => text.includes('other_private_tool'))).toBe(false)
    // 已选但跨 Agent 的工具进入警示条，可点击移除
    expect(wrapper.text()).toContain('不在本 Agent 可用范围')
    expect(wrapper.text()).toContain('属于 other-agent 的私有插件')
    wrapper.unmount()
  })

  it('saves after a clean validation and marks the agent pending', async () => {
    const wrapper = await mountPage()

    const saveButton = wrapper.findAll('button').find((b) => b.text().includes('保存'))
    await saveButton!.trigger('click')
    await flushPromises()

    expect(client.updateAgent).toHaveBeenCalledWith(
      'banking-agent',
      expect.objectContaining({ prompt_text: DETAIL.prompt_text }),
    )
    expect(wrapper.text()).toContain('待重启生效')
    wrapper.unmount()
  })

  it('renders the knowledge base section and serializes knowledge_bases on save', async () => {
    client.getAgent.mockResolvedValue({
      ...DETAIL,
      config: {
        ...DETAIL.config,
        knowledge_bases: [
          { name: 'pricing' },
          { name: 'service_flow', relevance_gate: { enabled: false } },
        ],
      },
    })
    const wrapper = await mountPage()

    const section = wrapper.get('#sec-kb')
    expect(section.text()).toContain('库默认 bm25')
    const checkboxes = section.findAll('input[type="checkbox"]')
    // pricing 已勾选；service_flow 已勾选但不在平台列表中仍保留显示
    expect((checkboxes[0]!.element as HTMLInputElement).checked).toBe(true)
    expect(section.text()).toContain('平台已不存在该知识库')

    const saveButton = wrapper.findAll('button').find((b) => b.text().includes('保存'))
    await saveButton!.trigger('click')
    await flushPromises()

    expect(client.updateAgent).toHaveBeenCalledWith(
      'banking-agent',
      expect.objectContaining({
        config: expect.objectContaining({
          knowledge_bases: [
            { name: 'pricing' },
            { name: 'service_flow', relevance_gate: { enabled: false } },
          ],
        }),
      }),
    )
    wrapper.unmount()
  })
})
