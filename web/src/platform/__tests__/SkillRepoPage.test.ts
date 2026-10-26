import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { createAppRouter } from '../../router'
import SkillRepoPage from '../SkillRepoPage.vue'

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

function skill(partial: Record<string, unknown>) {
  return {
    name: 'kb-writing-standard',
    description: '写作规范',
    source: 'global',
    metadata: {},
    resources: ['references/style.md'],
    valid: true,
    error: null,
    referenced_by: [],
    overridden_by: [],
    overrides_global: false,
    ...partial,
  }
}

async function mountPage() {
  const router = createAppRouter(createMemoryHistory())
  await router.push('/platform/skills')
  await router.isReady()
  const wrapper = mount(SkillRepoPage, { global: { plugins: [router] } })
  await flushPromises()
  return wrapper
}

describe('SkillRepoPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    client.listAgents.mockResolvedValue([
      {
        agent_id: 'banking-agent',
        agent_name: 'banking_agent',
        description: null,
        output_mode: 'stream',
        model: null,
        tool_count: 0,
        skill_count: 0,
        status: 'active',
        error: null,
      },
    ])
    client.listSkills.mockImplementation((scope: string) =>
      scope === 'global'
        ? Promise.resolve({
            skills: [
              skill({
                name: 'fraud-playbook',
                referenced_by: ['banking_golden'],
                overridden_by: ['banking-agent'],
              }),
            ],
            global_skills: null,
          })
        : Promise.resolve({
            skills: [
              skill({
                name: 'fraud-playbook',
                source: 'private',
                overrides_global: true,
              }),
            ],
            global_skills: null,
          }),
    )
    client.deleteSkill.mockResolvedValue(undefined)
  })

  it('lists global skills with override hints', async () => {
    const wrapper = await mountPage()

    expect(client.listSkills).toHaveBeenCalledWith('global')
    expect(wrapper.text()).toContain('fraud-playbook')
    expect(wrapper.text()).toContain('1 处被覆盖')
    expect(wrapper.text()).toContain('banking_golden')
    wrapper.unmount()
  })

  it('switches to the private tab and lists agent skills', async () => {
    const wrapper = await mountPage()

    await wrapper.get('.platform-libtab:nth-child(2)').trigger('click')
    await flushPromises()

    expect(client.listSkills).toHaveBeenCalledWith('banking-agent')
    expect(wrapper.text()).toContain('覆盖通用')
    wrapper.unmount()
  })

  it('requires typing the skill name before deleting a referenced global skill', async () => {
    const wrapper = await mountPage()

    await wrapper.get('.platform-btn-dngr').trigger('click')
    await flushPromises()
    const modal = document.querySelector('.platform-modal')
    expect(modal?.textContent).toContain('banking_golden')

    const confirm = [...document.querySelectorAll('.platform-modal button')].find((b) =>
      b.textContent?.includes('确认删除'),
    ) as HTMLButtonElement
    confirm.click()
    await flushPromises()
    expect(client.deleteSkill).not.toHaveBeenCalled()

    const input = document.querySelector('.platform-modal input') as HTMLInputElement
    input.value = 'fraud-playbook'
    input.dispatchEvent(new Event('input'))
    await flushPromises()
    confirm.click()
    await flushPromises()
    expect(client.deleteSkill).toHaveBeenCalledWith('global', 'fraud-playbook')
    wrapper.unmount()
  })
})
