import { enableAutoUnmount, mount } from '@vue/test-utils'
import { afterEach, describe, expect, it } from 'vitest'

import AgentPicker from './AgentPicker.vue'

enableAutoUnmount(afterEach)

const agent = {
  id: 'stream-agent',
  name: 'Stream Agent',
  description: 'Streams answers',
  outputMode: 'stream' as const,
}

describe('AgentPicker', () => {
  it('selects an Agent from the catalog by id', async () => {
    const other = { ...agent, id: 'other-agent', name: 'Other Agent' }
    const wrapper = mount(AgentPicker, {
      props: { agents: [agent, other], agent },
    })
    await wrapper.get('[aria-label="选择 Agent"]').trigger('click')
    expect(wrapper.get('details').element.open).toBe(true)
    await wrapper.get('[data-agent-id="other-agent"]').trigger('click')
    expect(wrapper.emitted('agent-change')).toEqual([[other.id]])
    expect(wrapper.get('details').element.open).toBe(false)
  })

  it('closes the picker on Escape and outside interaction', async () => {
    const wrapper = mount(AgentPicker, {
      attachTo: document.body,
      props: { agents: [agent], agent },
    })
    const trigger = wrapper.get('[aria-label="选择 Agent"]')
    await trigger.trigger('click')
    await trigger.trigger('keydown', { key: 'Escape' })
    expect(wrapper.get('details').element.open).toBe(false)
    await trigger.trigger('click')
    document.body.dispatchEvent(new Event('pointerdown', { bubbles: true }))
    expect(wrapper.get('details').element.open).toBe(false)
    wrapper.unmount()
  })
})
