import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory } from 'vue-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { createAppRouter } from '../router'
import ChatPage from './ChatPage.vue'

const { agentsMock, runtimeClient } = vi.hoisted(() => {
  const agents = vi.fn()
  return {
    agentsMock: agents,
    runtimeClient: {
      agents,
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
      events: vi.fn(),
    },
  }
})

vi.mock('../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/client')>()),
  createRuntimeClient: () => runtimeClient,
}))

const writer = {
  id: 'writer',
  name: 'Writer',
  description: 'Writes reports',
  outputMode: 'complete' as const,
}
const reader = {
  id: 'reader',
  name: 'Reader',
  description: 'Reads documents',
  outputMode: 'stream' as const,
}

async function mountChat(path = '/chat') {
  const router = createAppRouter(createMemoryHistory())
  await router.push(path)
  await router.isReady()
  return mount(ChatPage, { global: { plugins: [router] } })
}

describe('ChatPage Agent catalog bootstrap', () => {
  beforeEach(() => {
    localStorage.clear()
    sessionStorage.clear()
    vi.clearAllMocks()
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('shows a loading state while the Agent catalog is unresolved', async () => {
    agentsMock.mockReturnValueOnce(new Promise(() => undefined))

    const wrapper = await mountChat()
    await wrapper.vm.$nextTick()

    expect(wrapper.text()).toContain('正在加载 Agent')
    expect(wrapper.find('[aria-label="选择 Agent"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('shows an empty state without mounting chat', async () => {
    agentsMock.mockResolvedValueOnce([])

    const wrapper = await mountChat()
    await flushPromises()

    expect(wrapper.text()).toContain('暂无可用 Agent')
    expect(wrapper.find('[aria-label="选择 Agent"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('retries a failed catalog request and mounts chat once', async () => {
    agentsMock
      .mockRejectedValueOnce(new Error('catalog unavailable'))
      .mockResolvedValueOnce([writer])
    const wrapper = await mountChat()
    await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toContain('catalog unavailable')

    await wrapper.get('button').trigger('click')
    await flushPromises()

    expect(wrapper.findAll('[aria-label="选择 Agent"]')).toHaveLength(1)
    expect(wrapper.get('.agent-option').text()).toContain('Writer')
    wrapper.unmount()
  })

  it('mounts chat with the dynamically returned Agent', async () => {
    agentsMock.mockResolvedValueOnce([writer])

    const wrapper = await mountChat()
    await flushPromises()

    expect(wrapper.get('[data-agent-id="writer"]').attributes('aria-current')).toBe(
      'true',
    )
    expect(wrapper.text()).toContain('Writes reports')
    wrapper.unmount()
  })

  it('preselects the Agent from the ?agent= query', async () => {
    agentsMock.mockResolvedValueOnce([writer, reader])

    const wrapper = await mountChat('/chat?agent=reader')
    await flushPromises()
    await flushPromises()

    expect(wrapper.get('[data-agent-id="reader"]').attributes('aria-current')).toBe(
      'true',
    )
    wrapper.unmount()
  })
})
