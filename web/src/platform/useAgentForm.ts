import { reactive } from 'vue'

import type { Issue } from '../api/platformClient'

/** Agent 对单个知识库的引用行：勾选即引用，可按行覆盖检索/裁判配置。 */
export interface AgentKnowledgeRef {
  name: string
  /** 完整 retrieval 覆盖对象（含 embedding 的 ${VAR} 占位符），null = 跟随库默认 */
  retrieval: Record<string, unknown> | null
  /** 裁判覆盖：follow=跟随库默认；disable=显式关闭；custom=保留服务端原始配置 */
  gate: 'follow' | 'disable' | 'custom'
  gateCustom: Record<string, unknown> | null
}

/** Agent 编辑/向导共用的表单状态。 */
export interface AgentFormState {
  agentId: string
  agentName: string
  promptText: string
  model: string
  baseUrl: string
  apiKey: string
  temperature: number
  maxOutputTokens: number | null
  contextWindow: number | null
  timeoutSeconds: number | null
  parallelToolCalls: boolean
  enabledSkills: string[]
  enabledTools: string[]
  enabledMiddlewares: string[]
  enabledContextProviders: string[]
  knowledgeBases: AgentKnowledgeRef[]
  outputMode: 'stream' | 'complete'
  outboundType: string
  outboundOptionsText: string
  compactionEnabled: boolean
  triggerRatio: number
  recentMessageRatio: number
  safetyMarginTokens: number | null
  metadataText: string
  pluginOptionsText: string
  /** config 中未识别的字段，保存时原样合并回去，避免丢字段。 */
  extraConfig: Record<string, unknown>
}

const KNOWN_TOP_LEVEL_KEYS = new Set([
  'agent_id',
  'agent_name',
  'prompt',
  'enabled_skills',
  'enabled_tools',
  'enabled_middlewares',
  'enabled_context_providers',
  'knowledge_bases',
  'output_mode',
  'outbound',
  'llm_model',
  'compaction',
  'metadata',
  'plugin_options',
])

export function createEmptyForm(): AgentFormState {
  return {
    agentId: '',
    agentName: '',
    promptText: '',
    model: '',
    baseUrl: '',
    apiKey: '',
    temperature: 0.7,
    maxOutputTokens: null,
    contextWindow: null,
    timeoutSeconds: null,
    parallelToolCalls: false,
    enabledSkills: [],
    enabledTools: [],
    enabledMiddlewares: [],
    enabledContextProviders: [],
    knowledgeBases: [],
    outputMode: 'stream',
    outboundType: 'stream',
    outboundOptionsText: '',
    compactionEnabled: true,
    triggerRatio: 0.8,
    recentMessageRatio: 0.2,
    safetyMarginTokens: null,
    metadataText: '',
    pluginOptionsText: '',
    extraConfig: {},
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function stringValue(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

function numberValue(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function stringList(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((v): v is string => typeof v === 'string')
    : []
}

function jsonText(value: unknown): string {
  if (value === undefined || value === null) return ''
  return JSON.stringify(value, null, 2)
}

/** 把 config.knowledge_bases 解析为引用行；缺省覆盖字段映射为跟随库默认。 */
function knowledgeRefs(value: unknown): AgentKnowledgeRef[] {
  if (!Array.isArray(value)) return []
  const refs: AgentKnowledgeRef[] = []
  for (const entry of value) {
    if (!isRecord(entry)) continue
    const name = stringValue(entry.name)
    if (!name) continue
    let gate: AgentKnowledgeRef['gate'] = 'follow'
    let gateCustom: Record<string, unknown> | null = null
    if (isRecord(entry.relevance_gate)) {
      if (entry.relevance_gate.enabled === false) {
        gate = 'disable'
      } else {
        gate = 'custom'
        gateCustom = entry.relevance_gate
      }
    }
    refs.push({
      name,
      retrieval: isRecord(entry.retrieval) ? entry.retrieval : null,
      gate,
      gateCustom,
    })
  }
  return refs
}

/** 将服务端 config + prompt_text 映射为表单状态。 */
export function configToForm(
  config: Record<string, unknown>,
  promptText: string,
): AgentFormState {
  const form = createEmptyForm()
  form.agentId = stringValue(config.agent_id)
  form.agentName = stringValue(config.agent_name)
  form.promptText = promptText
  form.enabledSkills = stringList(config.enabled_skills)
  form.enabledTools = stringList(config.enabled_tools)
  form.enabledMiddlewares = stringList(config.enabled_middlewares)
  form.enabledContextProviders = stringList(config.enabled_context_providers)
  form.knowledgeBases = knowledgeRefs(config.knowledge_bases)
  form.outputMode = config.output_mode === 'complete' ? 'complete' : 'stream'
  if (isRecord(config.outbound)) {
    form.outboundType = stringValue(config.outbound.type) || 'stream'
    form.outboundOptionsText = jsonText(config.outbound.options)
  }
  if (isRecord(config.llm_model)) {
    const llm = config.llm_model
    form.model = stringValue(llm.model)
    form.baseUrl = stringValue(llm.base_url)
    form.apiKey = stringValue(llm.api_key)
    form.temperature = numberValue(llm.temperature) ?? 0.7
    form.maxOutputTokens = numberValue(llm.max_output_tokens)
    form.contextWindow = numberValue(llm.context_window)
    form.timeoutSeconds = numberValue(llm.timeout_seconds)
    form.parallelToolCalls = llm.parallel_tool_calls === true
  }
  if (isRecord(config.compaction)) {
    form.compactionEnabled = true
    form.triggerRatio = numberValue(config.compaction.trigger_ratio) ?? 0.8
    form.recentMessageRatio = numberValue(config.compaction.recent_message_ratio) ?? 0.2
    form.safetyMarginTokens = numberValue(config.compaction.safety_margin_tokens)
  } else {
    form.compactionEnabled = false
  }
  form.metadataText = jsonText(config.metadata)
  form.pluginOptionsText = jsonText(config.plugin_options)
  for (const [key, value] of Object.entries(config)) {
    if (!KNOWN_TOP_LEVEL_KEYS.has(key)) form.extraConfig[key] = value
  }
  return form
}

export interface FormToConfigResult {
  config: Record<string, unknown>
  jsonErrors: Issue[]
}

function parseJsonField(
  text: string,
  field: string,
  errors: Issue[],
): Record<string, unknown> | undefined {
  const trimmed = text.trim()
  if (!trimmed) return undefined
  try {
    const value: unknown = JSON.parse(trimmed)
    if (!isRecord(value)) {
      errors.push({ field, message: `${field} 必须是 JSON 对象` })
      return undefined
    }
    return value
  } catch {
    errors.push({ field, message: `${field} 不是合法 JSON` })
    return undefined
  }
}

/** 将表单状态序列化回 config dict（未知字段原样合并）。 */
export function formToConfig(form: AgentFormState): FormToConfigResult {
  const jsonErrors: Issue[] = []
  const llm: Record<string, unknown> = { type: 'litellm' }
  if (form.model.trim()) llm.model = form.model.trim()
  if (form.baseUrl.trim()) llm.base_url = form.baseUrl.trim()
  if (form.apiKey.trim()) llm.api_key = form.apiKey.trim()
  llm.temperature = form.temperature
  if (form.maxOutputTokens !== null) llm.max_output_tokens = form.maxOutputTokens
  if (form.contextWindow !== null) llm.context_window = form.contextWindow
  if (form.timeoutSeconds !== null) llm.timeout_seconds = form.timeoutSeconds
  llm.parallel_tool_calls = form.parallelToolCalls

  const outbound: Record<string, unknown> = { type: form.outboundType || 'stream' }
  const outboundOptions = parseJsonField(
    form.outboundOptionsText,
    'outbound.options',
    jsonErrors,
  )
  if (outboundOptions) outbound.options = outboundOptions

  const metadata = parseJsonField(form.metadataText, 'metadata', jsonErrors)
  const pluginOptions = parseJsonField(
    form.pluginOptionsText,
    'plugin_options',
    jsonErrors,
  )

  const config: Record<string, unknown> = {
    agent_id: form.agentId.trim(),
    agent_name: form.agentName.trim(),
    prompt: 'prompt/system.md',
    enabled_skills: [...form.enabledSkills],
    enabled_tools: [...form.enabledTools],
    enabled_middlewares: [...form.enabledMiddlewares],
    enabled_context_providers: [...form.enabledContextProviders],
    output_mode: form.outputMode,
    outbound,
    llm_model: llm,
  }
  if (form.compactionEnabled) {
    const compaction: Record<string, unknown> = {
      trigger_ratio: form.triggerRatio,
      recent_message_ratio: form.recentMessageRatio,
    }
    if (form.safetyMarginTokens !== null) {
      compaction.safety_margin_tokens = form.safetyMarginTokens
    }
    config.compaction = compaction
  }
  if (form.knowledgeBases.length) {
    config.knowledge_bases = form.knowledgeBases.map((ref) => {
      const entry: Record<string, unknown> = { name: ref.name }
      if (ref.retrieval) entry.retrieval = ref.retrieval
      if (ref.gate === 'disable') entry.relevance_gate = { enabled: false }
      if (ref.gate === 'custom' && ref.gateCustom) {
        entry.relevance_gate = ref.gateCustom
      }
      return entry
    })
  }
  if (metadata) config.metadata = metadata
  if (pluginOptions) config.plugin_options = pluginOptions
  for (const [key, value] of Object.entries(form.extraConfig)) {
    if (!(key in config)) config[key] = value
  }
  return { config, jsonErrors }
}

export const AGENT_ID_RE = /^[a-z0-9][a-z0-9-]*$/
const ENV_REF_RE = /^\$\{[A-Za-z_][A-Za-z0-9_]*\}$/

/** 前端本地校验规则（服务端 validate 之外的即时反馈）。 */
export function validateFormLocal(
  form: AgentFormState,
  options: { requirePrompt?: boolean; existingAgentIds?: string[] } = {},
): Issue[] {
  const issues: Issue[] = []
  const agentId = form.agentId.trim()
  if (!agentId) {
    issues.push({ field: 'agent_id', message: 'agent_id 不能为空' })
  } else if (!AGENT_ID_RE.test(agentId)) {
    issues.push({
      field: 'agent_id',
      message: 'agent_id 只能包含小写字母、数字和连字符，且以字母或数字开头',
    })
  } else if (options.existingAgentIds?.includes(agentId)) {
    issues.push({
      field: 'agent_id',
      message: `agent_id 与现有 Agent「${agentId}」重复`,
    })
  }
  if (!form.agentName.trim()) {
    issues.push({ field: 'agent_name', message: 'agent_name 不能为空' })
  }
  if (options.requirePrompt && !form.promptText.trim()) {
    issues.push({ field: 'prompt', message: '系统 Prompt 不能为空' })
  }
  const apiKey = form.apiKey.trim()
  if (apiKey && !ENV_REF_RE.test(apiKey)) {
    issues.push({
      field: 'llm_model.api_key',
      message: 'api_key 只接受 ${VAR} 环境变量引用格式或留空，平台不存储明文密钥',
    })
  }
  if (
    form.contextWindow !== null &&
    form.maxOutputTokens !== null &&
    form.maxOutputTokens >= form.contextWindow
  ) {
    issues.push({
      field: 'llm_model.context_window',
      message: `context_window（${form.contextWindow}）必须大于 max_output_tokens（${form.maxOutputTokens}）`,
    })
  }
  if (form.compactionEnabled && form.recentMessageRatio >= form.triggerRatio) {
    issues.push({
      field: 'compaction.recent_message_ratio',
      message: `recent_message_ratio（${form.recentMessageRatio}）必须小于 trigger_ratio（${form.triggerRatio}）`,
    })
  }
  const kbNames = form.knowledgeBases.map((ref) => ref.name)
  if (new Set(kbNames).size !== kbNames.length) {
    issues.push({ field: 'knowledge_bases', message: 'knowledge_bases 包含重复项' })
  }
  issues.push(...formToConfig(form).jsonErrors)
  return issues
}

/** 校验字段所属编辑区块，用于错误角标与点击跳转。 */
export function sectionForField(field: string): string {
  if (field.startsWith('llm_model')) return 'sec-model'
  if (field.startsWith('enabled_')) return 'sec-cap'
  if (field.startsWith('knowledge_bases')) return 'sec-kb'
  if (field === 'output_mode' || field.startsWith('outbound')) return 'sec-out'
  if (field.startsWith('compaction')) return 'sec-compact'
  if (field === 'metadata' || field === 'plugin_options' || field.startsWith('extra')) {
    return 'sec-adv'
  }
  if (field === 'prompt') return 'sec-prompt'
  return 'sec-basic'
}

export interface AgentForm {
  form: AgentFormState
  reset: (config?: Record<string, unknown>, promptText?: string) => void
  toConfig: () => FormToConfigResult
  localIssues: () => Issue[]
}

/** Agent 表单 composable：编辑页与向导共享同一份实现。 */
export function useAgentForm(): AgentForm {
  const form = reactive(createEmptyForm())
  return {
    form,
    reset(config, promptText = '') {
      Object.assign(form, config ? configToForm(config, promptText) : createEmptyForm())
    },
    toConfig: () => formToConfig(form),
    localIssues: () => validateFormLocal(form, { requirePrompt: true }),
  }
}
