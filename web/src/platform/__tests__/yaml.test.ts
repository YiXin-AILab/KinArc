import { describe, expect, it } from 'vitest'

import { highlightYaml, toYaml } from '../yaml'

describe('toYaml', () => {
  it('serializes nested objects and arrays in block style', () => {
    const yaml = toYaml({
      agent_id: 'loan-advisor',
      enabled_tools: ['KB_search', 'get_current_time'],
      llm_model: { type: 'litellm', temperature: 0.3, parallel_tool_calls: false },
      outbound: { type: 'stream' },
    })
    expect(yaml).toBe(
      [
        'agent_id: loan-advisor',
        'enabled_tools:',
        '  - KB_search',
        '  - get_current_time',
        'llm_model:',
        '  type: litellm',
        '  temperature: 0.3',
        '  parallel_tool_calls: false',
        'outbound:',
        '  type: stream',
      ].join('\n'),
    )
  })

  it('quotes strings that would parse as other types', () => {
    expect(toYaml({ value: 'true' })).toBe('value: "true"')
    expect(toYaml({ value: '123' })).toBe('value: "123"')
    expect(toYaml({ value: 'a: b' })).toBe('value: "a: b"')
    expect(toYaml({ value: '${OPENAI_API_KEY}' })).toBe('value: ${OPENAI_API_KEY}')
  })

  it('renders empty collections and null', () => {
    expect(toYaml({ skill_roots: [], options: {}, note: null })).toBe(
      ['skill_roots: []', 'options: {}', 'note: null'].join('\n'),
    )
  })
})

describe('highlightYaml', () => {
  it('escapes HTML before highlighting', () => {
    const html = highlightYaml('note: <script>alert(1)</script>')
    expect(html).not.toContain('<script>')
    expect(html).toContain('&lt;script&gt;')
  })

  it('marks keys, numbers and comments', () => {
    const html = highlightYaml('temperature: 0.3  # 温度')
    expect(html).toContain('<span class="yk">temperature</span>')
    expect(html).toContain('<span class="yn">0.3</span>')
    expect(html).toContain('<span class="yc"># 温度</span>')
  })
})
