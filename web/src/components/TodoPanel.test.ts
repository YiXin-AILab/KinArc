import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import TodoPanel from './TodoPanel.vue'

describe('TodoPanel', () => {
  it('hides empty lists and shows a retry action for read errors', async () => {
    const wrapper = mount(TodoPanel, {
      props: { todos: [], error: false, loading: false },
    })
    expect(wrapper.find('section').exists()).toBe(false)
    await wrapper.setProps({ error: true })
    await wrapper.get('button').trigger('click')
    expect(wrapper.emitted('retry')).toHaveLength(1)
  })

  it('shows progress, expands ordered plain text and retains completed tasks', async () => {
    const wrapper = mount(TodoPanel, {
      props: {
        todos: [
          { content: '确认需求', status: 'completed' },
          { content: '<b>实现</b>', status: 'in_progress' },
          { content: '验证', status: 'pending' },
        ],
        error: false,
        loading: false,
      },
    })
    expect(wrapper.text()).toContain('1 / 3')
    expect(wrapper.text()).toContain('<b>实现</b>')
    expect(wrapper.find('ol').exists()).toBe(false)
    await wrapper.get('.todo-toggle').trigger('click')
    expect(wrapper.get('.todo-toggle').attributes('aria-expanded')).toBe('true')
    expect(wrapper.findAll('li')).toHaveLength(3)
    expect(wrapper.find('b').exists()).toBe(false)
    expect(wrapper.text()).toContain('进行中')
    await wrapper.setProps({ todos: [{ content: '确认需求', status: 'completed' }] })
    expect(wrapper.text()).toContain('已完成 1 / 1')
    expect(wrapper.findAll('li')).toHaveLength(1)
  })

  it('summarizes concurrent tasks without the status label and animates active items', async () => {
    const wrapper = mount(TodoPanel, {
      props: {
        todos: [
          { content: '第一项很长的任务', status: 'in_progress' },
          { content: '第二项', status: 'in_progress' },
        ],
        error: false,
        loading: false,
      },
    })
    expect(wrapper.get('.todo-summary').text()).toBe('2 项 · 第一项很长的任务')
    expect(wrapper.text()).not.toContain('进行中')
    expect(wrapper.find('.todo-spinner').exists()).toBe(true)
    await wrapper.get('.todo-toggle').trigger('click')
    expect(wrapper.find('.todo-summary').exists()).toBe(false)
    expect(wrapper.findAll('.todo-spinner')).toHaveLength(2)
    expect(wrapper.get('.todo-content').attributes('title')).toBe('第一项很长的任务')
    await wrapper.setProps({ todos: [{ content: '完成', status: 'completed' }] })
    expect(wrapper.find('.todo-spinner').exists()).toBe(false)
  })
})
