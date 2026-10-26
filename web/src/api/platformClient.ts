import { createHttpTransport } from './httpTransport'

export type AgentStatus = 'active' | 'pending' | 'invalid'
export type PlatformOutputMode = 'complete' | 'stream'

export interface AgentSummary {
  agent_id: string
  agent_name: string
  description: string | null
  output_mode: PlatformOutputMode
  model: string | null
  tool_count: number
  skill_count: number
  status: AgentStatus
  error: string | null
}

export interface AgentDetail {
  config: Record<string, unknown>
  prompt_text: string
  status: string
  error: string | null
}

export interface AgentPayload {
  config: Record<string, unknown>
  prompt_text: string
}

export interface Issue {
  field: string
  message: string
}

export interface ValidateResult {
  errors: Issue[]
  warnings: Issue[]
}

export interface CatalogItem {
  name: string
  source: 'builtin' | 'application'
  description: string | null
  /** null/缺省 = builtin 全局可用；非 null = 归属某个 Agent 的私有插件，不可跨 Agent 使用。 */
  agent_id?: string | null
}

export interface Catalog {
  tools: CatalogItem[]
  middlewares: CatalogItem[]
  context_providers: CatalogItem[]
  outbounds: { type: string }[]
  global_skills: { name: string; description: string }[]
}

export interface SkillSummary {
  name: string
  description: string
  source: 'global' | 'private'
  metadata: Record<string, string>
  resources: string[]
  valid: boolean
  error: string | null
  referenced_by: string[]
  overridden_by: string[]
  overrides_global: boolean
}

export interface SkillListResult {
  skills: SkillSummary[]
  global_skills: SkillSummary[] | null
}

export interface SkillPayload {
  scope: string
  name: string
  description: string
  content: string
  metadata?: Record<string, string>
  allowed_tools?: string[]
}

export interface SkillDetail {
  name: string
  description: string
  metadata: Record<string, string>
  allowed_tools: string[]
  content: string
  resources: string[]
}

export interface SkillUpdatePayload {
  description: string
  content: string
  metadata: Record<string, string>
  allowed_tools: string[]
}

export interface SkillResource {
  path: string
  size: number
}

export interface PlatformClientConfig {
  apiBaseUrl: string
  tokenProvider?: () => string | null
}

export interface PlatformClient {
  listAgents(signal?: AbortSignal): Promise<AgentSummary[]>
  createAgent(payload: AgentPayload, signal?: AbortSignal): Promise<AgentSummary>
  getAgent(agentId: string, signal?: AbortSignal): Promise<AgentDetail>
  updateAgent(
    agentId: string,
    payload: AgentPayload,
    signal?: AbortSignal,
  ): Promise<AgentSummary>
  deleteAgent(agentId: string, signal?: AbortSignal): Promise<void>
  validateAgent(
    payload: { config: Record<string, unknown>; prompt_text: string | null },
    signal?: AbortSignal,
  ): Promise<ValidateResult>
  catalog(signal?: AbortSignal): Promise<Catalog>
  listSkills(scope: string, signal?: AbortSignal): Promise<SkillListResult>
  createSkill(payload: SkillPayload, signal?: AbortSignal): Promise<SkillSummary>
  getSkill(scope: string, name: string, signal?: AbortSignal): Promise<SkillDetail>
  updateSkill(
    scope: string,
    name: string,
    payload: SkillUpdatePayload,
    signal?: AbortSignal,
  ): Promise<SkillSummary>
  deleteSkill(scope: string, name: string, signal?: AbortSignal): Promise<void>
  listSkillResources(
    scope: string,
    name: string,
    signal?: AbortSignal,
  ): Promise<SkillResource[]>
  putSkillResource(
    scope: string,
    name: string,
    path: string,
    content: string,
    signal?: AbortSignal,
  ): Promise<void>
  /** 契约外的补充端点：读取单个资源内容，后端未实现时调用方需降级。 */
  getSkillResource(
    scope: string,
    name: string,
    path: string,
    signal?: AbortSignal,
  ): Promise<string>
  deleteSkillResource(
    scope: string,
    name: string,
    path: string,
    signal?: AbortSignal,
  ): Promise<void>
  skillReferences(name: string, signal?: AbortSignal): Promise<string[]>
}

export function createPlatformClient(
  config: PlatformClientConfig,
  fetchImpl: typeof fetch = fetch,
): PlatformClient {
  const base = `${config.apiBaseUrl}/v1/platform`
  const transport = createHttpTransport(config, fetchImpl)

  const url = (path: string): string => `${base}${path}`

  const skillUrl = (scope: string, name: string, suffix = ''): string =>
    url(`/skills/${encodeURIComponent(scope)}/${encodeURIComponent(name)}${suffix}`)

  const request = <T>(
    path: string,
    options: {
      method?: string
      body?: unknown
      signal?: AbortSignal
    } = {},
  ): Promise<T> => transport.requestJson<T>(url(path), options)

  const requestSkill = <T>(
    scope: string,
    name: string,
    suffix: string,
    options: { method?: string; body?: unknown; signal?: AbortSignal } = {},
  ): Promise<T> => transport.requestJson<T>(skillUrl(scope, name, suffix), options)

  return {
    async listAgents(signal) {
      const body = await request<{ agents: AgentSummary[] }>('/agents', { signal })
      return body.agents
    },
    async createAgent(payload, signal) {
      const body = await request<{ agent: AgentSummary }>('/agents', {
        method: 'POST',
        body: payload,
        signal,
      })
      return body.agent
    },
    getAgent(agentId, signal) {
      return request<AgentDetail>(`/agents/${encodeURIComponent(agentId)}`, {
        signal,
      })
    },
    async updateAgent(agentId, payload, signal) {
      const body = await request<{ agent: AgentSummary }>(
        `/agents/${encodeURIComponent(agentId)}`,
        { method: 'PUT', body: payload, signal },
      )
      return body.agent
    },
    async deleteAgent(agentId, signal) {
      await request<{ deleted: boolean }>(`/agents/${encodeURIComponent(agentId)}`, {
        method: 'DELETE',
        signal,
      })
    },
    validateAgent(payload, signal) {
      return request<ValidateResult>('/agents/validate', {
        method: 'POST',
        body: payload,
        signal,
      })
    },
    catalog(signal) {
      return request<Catalog>('/catalog', { signal })
    },
    listSkills(scope, signal) {
      return request<SkillListResult>(`/skills?scope=${encodeURIComponent(scope)}`, {
        signal,
      })
    },
    async createSkill(payload, signal) {
      const body = await request<{ skill: SkillSummary }>('/skills', {
        method: 'POST',
        body: payload,
        signal,
      })
      return body.skill
    },
    getSkill(scope, name, signal) {
      return requestSkill<SkillDetail>(scope, name, '', { signal })
    },
    async updateSkill(scope, name, payload, signal) {
      const body = await requestSkill<{ skill: SkillSummary }>(scope, name, '', {
        method: 'PUT',
        body: payload,
        signal,
      })
      return body.skill
    },
    async deleteSkill(scope, name, signal) {
      await requestSkill<{ deleted: boolean }>(scope, name, '', {
        method: 'DELETE',
        signal,
      })
    },
    async listSkillResources(scope, name, signal) {
      const body = await requestSkill<{ resources: SkillResource[] }>(
        scope,
        name,
        '/resources',
        { signal },
      )
      return body.resources
    },
    async putSkillResource(scope, name, path, content, signal) {
      await requestSkill<{ path: string }>(
        scope,
        name,
        `/resources/${encodeURIComponent(path)}`,
        { method: 'PUT', body: { content }, signal },
      )
    },
    async getSkillResource(scope, name, path, signal) {
      const body = await requestSkill<{ content: string }>(
        scope,
        name,
        `/resources/${encodeURIComponent(path)}`,
        { signal },
      )
      return body.content
    },
    async deleteSkillResource(scope, name, path, signal) {
      await requestSkill<{ deleted: boolean }>(
        scope,
        name,
        `/resources/${encodeURIComponent(path)}`,
        { method: 'DELETE', signal },
      )
    },
    async skillReferences(name, signal) {
      const body = await request<{ agent_ids: string[] }>(
        `/skills/${encodeURIComponent(name)}/references`,
        { signal },
      )
      return body.agent_ids
    },
  }
}
