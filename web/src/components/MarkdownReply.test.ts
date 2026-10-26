import { enableAutoUnmount, mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'
import MarkdownReply from './MarkdownReply.vue'

enableAutoUnmount(afterEach)

describe('assistant Markdown', () => {
  it('renders formulas without allowing trusted HTML or JavaScript links', async () => {
    const wrapper = mount(MarkdownReply, {
      props: {
        streaming: false,
        content:
          '$$\n\\frac{1}{2}\n$$\n\n$$\n\\href{javascript:alert(1)}{unsafe}\n$$\n\n$$\n\\htmlClass{injected}{unsafe}\n$$',
      },
    })
    await vi.waitFor(() => expect(wrapper.find('.katex .mfrac').exists()).toBe(true))
    await vi.waitFor(() => expect(wrapper.findAll('.katex')).toHaveLength(3))
    expect(wrapper.find('a[href^="javascript:"], .injected, script').exists()).toBe(
      false,
    )
  })

  it('preserves unfinished code when generation stops and when restored from history', async () => {
    const content = '```unknown-language\nfirst line\n\n    last line'
    const live = mount(MarkdownReply, { props: { content, streaming: true } })
    await vi.waitFor(() => expect(live.find('pre').exists()).toBe(true))
    await live.setProps({ streaming: false })
    const history = mount(MarkdownReply, { props: { content, streaming: false } })
    await vi.waitFor(() => expect(history.find('pre').exists()).toBe(true))
    expect(live.get('pre').text()).toBe(history.get('pre').text())
    expect(
      live
        .findAll('[data-stream-markdown="code-line"]')
        .map((line) => line.element.textContent),
    ).toEqual(['first line', '', '    last line'])
  })

  it('updates a partial table without duplicating rows or losing inline formatting', async () => {
    const wrapper = mount(MarkdownReply, {
      props: { streaming: true, content: '| Key | Value |\n| --- | --- |\n| a |' },
    })
    await wrapper.setProps({
      content: '| Key | Value |\n| --- | --- |\n| a | **bold** |\n| b | `code` |',
      streaming: false,
    })
    await vi.waitFor(() => expect(wrapper.findAll('tbody tr')).toHaveLength(2))
    expect(wrapper.get('td strong').text()).toBe('bold')
    expect(wrapper.get('td code').text()).toBe('code')
  })

  it('does not render active HTML from raw tags or HTML code previews', async () => {
    const wrapper = mount(MarkdownReply, {
      props: {
        streaming: false,
        content:
          '<img src=x onerror="alert(1)">\n\n<iframe src="https://example.com"></iframe>\n\n```html\n<script>alert(1)</script>\n```',
      },
    })
    await vi.waitFor(() => expect(wrapper.find('pre').exists()).toBe(true))
    expect(wrapper.find('iframe, script, img[onerror]').exists()).toBe(false)
    expect(wrapper.get('pre').text()).toContain('<script>alert(1)</script>')
  })

  it('renders tables and emphasis while suppressing executable HTML and unsafe links', async () => {
    const wrapper = mount(MarkdownReply, {
      props: {
        streaming: false,
        content:
          '**Hello**\n\n| Key | Value |\n| --- | --- |\n| a | b |\n\n<script>alert(1)</script>\n\n[bad](javascript:alert%281%29)',
      },
    })
    await vi.waitFor(() => expect(wrapper.find('table').exists()).toBe(true))
    expect(wrapper.get('strong').text()).toBe('Hello')
    expect(wrapper.find('script, iframe, [onclick], [onerror]').exists()).toBe(false)
    expect(wrapper.find('a[href^="javascript:"]').exists()).toBe(false)
  })

  it('finishes an open streaming code fence with highlighting and copies raw code', async () => {
    const copied: string[] = []
    const original = document.execCommand
    document.execCommand = vi.fn(() => {
      copied.push(document.querySelector('textarea')?.value ?? '')
      return true
    })
    const wrapper = mount(MarkdownReply, {
      props: { streaming: true, content: '```javascript\nconst answer = ' },
    })
    try {
      await vi.waitFor(() => expect(wrapper.find('pre').exists()).toBe(true))
      await wrapper.setProps({
        content: '```javascript\nconst answer = 42;\n```',
        streaming: false,
      })
      await vi.waitFor(() =>
        expect(wrapper.get('pre').text()).toContain('const answer = 42;'),
      )
      await vi.waitFor(() =>
        expect(wrapper.find('pre span[style]').exists()).toBe(true),
      )
      const copy = wrapper
        .findAll('button')
        .find((button) =>
          /复制|copy/i.test(
            button.attributes('aria-label') ?? button.attributes('title') ?? '',
          ),
        )
      expect(copy).toBeDefined()
      await copy!.trigger('click')
      await vi.waitFor(() => expect(copied).toHaveLength(1))
      expect(copied[0]?.trim()).toBe('const answer = 42;')
    } finally {
      document.execCommand = original
    }
  })
})
