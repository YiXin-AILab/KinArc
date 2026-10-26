import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory } from 'vue-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import App from './App.vue'
import { createAppRouter } from './router'

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

vi.mock('./api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('./api/client')>()),
  createRuntimeClient: () => runtimeClient,
}))

async function mountApp(path = '/') {
  const router = createAppRouter(createMemoryHistory())
  await router.push(path)
  await router.isReady()
  const wrapper = mount(App, { global: { plugins: [router] } })
  await flushPromises()
  return wrapper
}

describe('App 平台导航', () => {
  beforeEach(() => {
    localStorage.clear()
    sessionStorage.clear()
    vi.clearAllMocks()
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('renders nav tabs and redirects / to /chat', async () => {
    agentsMock.mockResolvedValueOnce([])
    const wrapper = await mountApp('/')

    expect(wrapper.text()).toContain('对话')
    expect(wrapper.text()).toContain('Agent 管理')
    expect(wrapper.text()).toContain('Skill 仓库')
    expect(wrapper.find('.platform-nav').exists()).toBe(true)
    // / 重定向到 /chat，ChatPage 的空态出现在路由视图里
    expect(wrapper.text()).toContain('暂无可用 Agent')
    wrapper.unmount()
  })

  it('highlights the active tab for platform routes', async () => {
    agentsMock.mockResolvedValue([])
    const wrapper = await mountApp('/platform/agents')
    await flushPromises()

    const active = wrapper.find('.platform-nav-tab.router-link-active')
    expect(active.exists()).toBe(true)
    expect(active.text()).toContain('Agent 管理')
    wrapper.unmount()
  })
})
