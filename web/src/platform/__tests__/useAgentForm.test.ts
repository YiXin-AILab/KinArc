import { describe, expect, it } from 'vitest'

import {
  configToForm,
  createEmptyForm,
  formToConfig,
  sectionForField,
  validateFormLocal,
} from '../useAgentForm'

describe('configToForm / formToConfig', () => {
  it('round-trips known fields and preserves unknown keys', () => {
    const config = {
      agent_id: 'banking-agent',
      agent_name: 'banking_agent',
      prompt: 'prompt/system.md',
      enabled_skills: ['kb-writing-standard'],
      enabled_tools: ['KB_search'],
      enabled_middlewares: ['system-prompt'],
      enabled_context_providers: [],
      output_mode: 'stream',
      outbound: { type: 'stream', options: { include_thinking: true } },
      llm_model: {
        type: 'litellm',
        model: 'claude-sonnet-4',
        base_url: 'https://example/v1',
        api_key: '${TEST_KEY}',
        temperature: 0,
        max_output_tokens: 8192,
        context_window: 128000,
        timeout_seconds: 120,
        parallel_tool_calls: true,
      },
      compaction: {
        trigger_ratio: 0.9,
        recent_message_ratio: 0.1,
        safety_margin_tokens: 2048,
      },
      metadata: { team: 'banking' },
      plugin_options: { discoverable: { auto_unlock: false } },
      future_field: { nested: [1, 2] },
    }
    const form = configToForm(config, '# 角色')
    expect(form.agentId).toBe('banking-agent')
    expect(form.promptText).toBe('# 角色')
    expect(form.parallelToolCalls).toBe(true)
    expect(form.extraConfig).toEqual({ future_field: { nested: [1, 2] } })

    const { config: out, jsonErrors } = formToConfig(form)
    expect(jsonErrors).toEqual([])
    expect(out.agent_id).toBe('banking-agent')
    expect(out.llm_model).toMatchObject({
      type: 'litellm',
      model: 'claude-sonnet-4',
      api_key: '${TEST_KEY}',
      temperature: 0,
      max_output_tokens: 8192,
      context_window: 128000,
      timeout_seconds: 120,
      parallel_tool_calls: true,
    })
    expect(out.outbound).toEqual({
      type: 'stream',
      options: { include_thinking: true },
    })
    expect(out.compaction).toEqual({
      trigger_ratio: 0.9,
      recent_message_ratio: 0.1,
      safety_margin_tokens: 2048,
    })
    expect(out.metadata).toEqual({ team: 'banking' })
    expect(out.plugin_options).toEqual({ discoverable: { auto_unlock: false } })
    expect(out.future_field).toEqual({ nested: [1, 2] })
  })

  it('omits compaction when disabled', () => {
    const form = createEmptyForm()
    form.compactionEnabled = false
    const { config } = formToConfig(form)
    expect('compaction' in config).toBe(false)
  })

  it('round-trips knowledge_bases refs with retrieval and gate overrides', () => {
    const config = {
      agent_id: 'a',
      agent_name: 'a',
      knowledge_bases: [
        { name: 'pricing' },
        {
          name: 'vip',
          retrieval: {
            strategy: 'vector',
            top_k: 8,
            embedding: { model: 'emb', api_key: '${EMB_KEY}' },
          },
          relevance_gate: { enabled: false },
        },
      ],
    }
    const form = configToForm(config, '')
    expect(form.knowledgeBases).toHaveLength(2)
    expect(form.knowledgeBases[0]).toMatchObject({
      name: 'pricing',
      retrieval: null,
      gate: 'follow',
    })
    expect(form.knowledgeBases[1]).toMatchObject({ name: 'vip', gate: 'disable' })
    expect(form.extraConfig).toEqual({})

    const { config: out, jsonErrors } = formToConfig(form)
    expect(jsonErrors).toEqual([])
    expect(out.knowledge_bases).toEqual(config.knowledge_bases)
  })

  it('flags duplicate knowledge_bases names', () => {
    const form = createEmptyForm()
    form.agentId = 'a'
    form.agentName = 'a'
    form.knowledgeBases = [
      { name: 'pricing', retrieval: null, gate: 'follow', gateCustom: null },
      { name: 'pricing', retrieval: null, gate: 'follow', gateCustom: null },
    ]
    const issues = validateFormLocal(form)
    expect(issues.map((issue) => issue.field)).toContain('knowledge_bases')
  })

  it('reports JSON parse errors instead of throwing', () => {
    const form = createEmptyForm()
    form.metadataText = '{ invalid'
    const { jsonErrors } = formToConfig(form)
    expect(jsonErrors).toEqual([
      { field: 'metadata', message: 'metadata 不是合法 JSON' },
    ])
  })
})

describe('validateFormLocal', () => {
  it('accepts empty or ${VAR} api_key and rejects plain secrets', () => {
    const form = createEmptyForm()
    form.agentId = 'a'
    form.agentName = 'a'
    form.apiKey = ''
    expect(validateFormLocal(form)).toEqual([])
    form.apiKey = '${OPENAI_API_KEY}'
    expect(validateFormLocal(form)).toEqual([])
    form.apiKey = 'sk-plain-secret'
    const issues = validateFormLocal(form)
    expect(issues.map((issue) => issue.field)).toContain('llm_model.api_key')
  })

  it('requires context_window greater than max_output_tokens', () => {
    const form = createEmptyForm()
    form.agentId = 'a'
    form.agentName = 'a'
    form.maxOutputTokens = 132768
    form.contextWindow = 128000
    const issues = validateFormLocal(form)
    expect(issues.map((issue) => issue.field)).toContain('llm_model.context_window')
  })

  it('checks compaction ratio ordering and agent_id duplicates', () => {
    const form = createEmptyForm()
    form.agentId = 'banking-agent'
    form.agentName = 'a'
    form.triggerRatio = 0.5
    form.recentMessageRatio = 0.5
    const issues = validateFormLocal(form, {
      existingAgentIds: ['banking-agent'],
    })
    const fields = issues.map((issue) => issue.field)
    expect(fields).toContain('agent_id')
    expect(fields).toContain('compaction.recent_message_ratio')
  })
})

describe('sectionForField', () => {
  it('maps fields to editor sections', () => {
    expect(sectionForField('llm_model.model')).toBe('sec-model')
    expect(sectionForField('enabled_tools')).toBe('sec-cap')
    expect(sectionForField('outbound.type')).toBe('sec-out')
    expect(sectionForField('compaction.trigger_ratio')).toBe('sec-compact')
    expect(sectionForField('metadata')).toBe('sec-adv')
    expect(sectionForField('prompt')).toBe('sec-prompt')
    expect(sectionForField('agent_id')).toBe('sec-basic')
  })
})
