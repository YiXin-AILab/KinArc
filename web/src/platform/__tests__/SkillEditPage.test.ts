import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { createAppRouter } from '../../router'
import SkillEditPage from '../SkillEditPage.vue'

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

const DETAIL = {
  name: 'fraud-playbook',
  description: '欺诈处置流程',
  metadata: { version: '2.3' },
  allowed_tools: ['KB_search'],
  content: '# 欺诈处置流程',
  resources: ['references/escalation.md'],
}

async function mountPage(path: string) {
  const router = createAppRouter(createMemoryHistory())
  await router.push(path)
  await router.isReady()
  const wrapper = mount(SkillEditPage, { global: { plugins: [router] } })
  await flushPromises()
  return wrapper
}

describe('SkillEditPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    client.getSkill.mockResolvedValue(DETAIL)
    client.listSkillResources.mockResolvedValue([
      { path: 'references/escalation.md', size: 4200 },
    ])
    client.updateSkill.mockResolvedValue({ name: 'fraud-playbook' })
    client.createSkill.mockResolvedValue({ name: 'new-skill' })
    client.putSkillResource.mockResolvedValue(undefined)
  })

  it('loads frontmatter with a locked name and lists resources', async () => {
    const wrapper = await mountPage('/platform/skills/global/fraud-playbook/edit')

    const nameInput = wrapper.get<HTMLInputElement>('[aria-label="skill name"]')
    expect(nameInput.element.disabled).toBe(true)
    expect(nameInput.element.value).toBe('fraud-playbook')
    expect(
      wrapper.get<HTMLTextAreaElement>('[aria-label="skill description"]').element
        .value,
    ).toBe('欺诈处置流程')
    expect(wrapper.text()).toContain('references/escalation.md')
    expect(wrapper.text()).toContain('4.1 KB')

    const saveButton = wrapper.findAll('button').find((b) => b.text().includes('保存'))
    await saveButton!.trigger('click')
    await flushPromises()
    expect(client.updateSkill).toHaveBeenCalledWith(
      'global',
      'fraud-playbook',
      expect.objectContaining({
        metadata: { version: '2.3' },
        allowed_tools: ['KB_search'],
      }),
    )
    wrapper.unmount()
  })

  it('rejects unsafe resource paths', async () => {
    const wrapper = await mountPage('/platform/skills/global/fraud-playbook/edit')

    const newButton = wrapper
      .findAll('button')
      .find((b) => b.text().includes('新建文本文件'))
    await newButton!.trigger('click')
    await flushPromises()

    const pathInput = document.querySelector(
      '.platform-modal [aria-label="资源路径"]',
    ) as HTMLInputElement
    pathInput.value = '../escape.md'
    pathInput.dispatchEvent(new Event('input'))
    await flushPromises()
    const save = [...document.querySelectorAll('.platform-modal button')].find(
      (b) => b.textContent === '保存',
    ) as HTMLButtonElement
    save.click()
    await flushPromises()

    expect(client.putSkillResource).not.toHaveBeenCalled()
    expect(document.querySelector('.platform-modal')?.textContent).toContain('禁止')
    wrapper.unmount()
  })

  it('creates a new skill in create mode', async () => {
    const wrapper = await mountPage('/platform/skills/new?scope=banking-agent')

    const nameInput = wrapper.get<HTMLInputElement>('[aria-label="skill name"]')
    expect(nameInput.element.disabled).toBe(false)
    await nameInput.setValue('new-skill')
    await wrapper.get('[aria-label="skill description"]').setValue('描述')

    const saveButton = wrapper.findAll('button').find((b) => b.text().includes('保存'))
    await saveButton!.trigger('click')
    await flushPromises()

    expect(client.createSkill).toHaveBeenCalledWith(
      expect.objectContaining({ scope: 'banking-agent', name: 'new-skill' }),
    )
    wrapper.unmount()
  })
})
