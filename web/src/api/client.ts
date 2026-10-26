/**
 * Runtime 公开 API 的 HTTP 适配层。
 *
 * 负责 URL 编码、鉴权请求头、请求序列化和 HTTP 错误映射。
 * 为请求设置有限超时；取消和重试策略由调用方负责。
 * 大多数 JSON 结果仅作类型断言，不进行运行时结构校验；Agent 目录会显式校验。
 */

import type {
  AssetUploadResponse,
  InputContentBlock,
  MessageSubmissionOptions,
  PendingInput,
  PublicMessage,
  SessionSnapshot,
  SessionRecord,
  SessionPage,
  SubmissionMode,
  TodoItem,
} from '../domain/types'
import type { AgentDefinition, OutputMode } from '../config'

const REQUEST_TIMEOUT_MS = 30_000
const ASSET_TIMEOUT_MS = 120_000

/** 目录和标题接口返回的传输数据结构。 */
interface SessionInfoResponse {
  session_id: string
  agent_id: string
  title: string
  created_at: string
  updated_at: string
  title_version: number
}

/** 映射后端元数据，不推导标题或改变服务端顺序。 */
function sessionRecord(record: SessionInfoResponse): SessionRecord {
  return {
    sessionId: record.session_id,
    agentId: record.agent_id,
    title: record.title,
    createdAt: record.created_at,
    updatedAt: record.updated_at,
    titleVersion: record.title_version,
  }
}

/**
 * 请求携带的会话和 Agent 身份；应用和用户身份由客户端保存。
 */
export interface RuntimeScope {
  sessionId: string
  agentId: string
}

/**
 * 传输配置；每次请求都会调用 tokenProvider，以支持 Token 更新。
 */
export interface RuntimeClientConfig {
  apiBaseUrl: string
  appId: string
  userId: string
  tokenProvider?: () => string | null
}

/**
 * 提交数据包含调用方生成的消息 ID，用于对齐本地和服务端状态。
 */
export interface SubmitMessageInput extends MessageSubmissionOptions {
  messageId: string
  content: InputContentBlock[]
  mode: SubmissionMode
}

/**
 * 公开 API 传输协议；方法传播网络及 HTTP 错误，并遵循传入的取消信号。
 *
 * 提交完成仅表示 HTTP 请求获接受，不表示运行结束。观察、退避和生命周期状态
 * 由对话控制器管理。普通请求超时 30 秒，资产请求超时 120 秒，均包含响应体读取。
 * SSE 只限制建连时间；连接建立后仍由调用方取消。
 */
export interface RuntimeClient {
  /** 按创建顺序列出当前用户范围内的对话。 */
  sessions(cursor?: string, signal?: AbortSignal): Promise<SessionPage>
  /** 软删除对话；活跃会话冲突向调用方传播。 */
  deleteSession(scope: RuntimeScope, signal?: AbortSignal): Promise<void>
  /** 保存手动标题并返回权威的带版本记录。 */
  renameSession(
    scope: RuntimeScope,
    title: string,
    signal?: AbortSignal,
  ): Promise<SessionRecord>
  /** 生成并保存标题；冲突和模型失败向调用方传播。 */
  generateSessionTitle(
    scope: RuntimeScope,
    signal?: AbortSignal,
  ): Promise<SessionRecord>
  /** 获取并校验真实 Agent 目录；无效条目会使请求失败。 */
  agents(signal?: AbortSignal): Promise<AgentDefinition[]>
  /** 以 multipart 格式上传文件并返回服务端资产回执。 */
  uploadAsset(file: File, signal?: AbortSignal): Promise<AssetUploadResponse>
  /** 以 Blob 读取需鉴权的资产；调用方负责创建的对象 URL。 */
  readAsset(assetId: string, signal?: AbortSignal): Promise<Blob>
  /** 使用指定消息 ID 提交内容块；获接受即返回，无需等待运行结束。 */
  submitMessage(
    scope: RuntimeScope,
    input: SubmitMessageInput,
    signal?: AbortSignal,
  ): Promise<void>
  /** 使用新的恢复请求 ID 提交恢复 Token 和输入。 */
  resume(
    scope: RuntimeScope,
    resumeToken: string,
    input: Record<string, unknown>,
    signal?: AbortSignal,
  ): Promise<void>
  /** 请求取消；调用方须另行观察服务端最终状态。 */
  cancel(scope: RuntimeScope, signal?: AbortSignal): Promise<void>
  /** 读取权威执行状态，不修改会话。 */
  snapshot(scope: RuntimeScope, signal?: AbortSignal): Promise<SessionSnapshot>
  /** 按服务端顺序读取持久化的公开消息。 */
  messages(scope: RuntimeScope, signal?: AbortSignal): Promise<PublicMessage[]>
  /** 读取已接受但未领取的输入，并将传输字段名映射为前端字段名。 */
  pendingInputs(scope: RuntimeScope, signal?: AbortSignal): Promise<PendingInput[]>
  /** 读取完整的权威待办列表；不存在时视为空列表。 */
  todos(scope: RuntimeScope, signal?: AbortSignal): Promise<TodoItem[]>
  /**
   * 打开可续连的 SSE 响应，不读取响应体。
   *
   * @param scope - 流所属的会话及 Agent 身份。
   * @param cursor - 最后应用的传输 ID；首次连接时省略。
   * @param signal - 由观察方管理的取消信号。
   * @returns 响应体可读取的成功 text/event-stream 响应。
   * @throws RuntimeHttpError HTTP 失败、内容类型错误或响应体缺失时抛出。
   */
  events(
    scope: RuntimeScope,
    cursor: string | undefined,
    signal: AbortSignal,
  ): Promise<Response>
}

/**
 * HTTP 失败信息，保留状态码、后端错误码和可选重试提示。
 */
export class RuntimeHttpError extends Error {
  /**
   * 保留传输细节，不暴露原始响应体。
   *
   * @param status - HTTP 状态码；成功但无效的 SSE 响应也会保留。
   * @param code - 后端错误码或适配层生成的分类。
   * @param message - 可显示的错误描述。
   * @param retryAfterSeconds - 可用时提供的数值型重试提示。
   */
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly retryAfterSeconds?: number,
  ) {
    super(message)
    this.name = 'RuntimeHttpError'
  }
}

/**
 * 创建共享连接身份、按请求获取鉴权信息的 HTTP 客户端。
 *
 * @param config - API 基础 URL、身份及可选 Token 提供器。
 * @param fetchImpl - Fetch 实现，可在传输协议测试中注入。
 * @returns 传播取消、网络和解析失败的请求方法。
 * @remarks 此工厂不主动发起请求，也不实现自动重试。
 */
export function createRuntimeClient(
  config: RuntimeClientConfig,
  fetchImpl: typeof fetch = fetch,
): RuntimeClient {
  /**
   * 将 fetch 与响应体读取纳入同一超时边界，同时保留调用方取消能力。
   * SSE 在收到响应头后返回，因此已建立的流没有总时长限制。
   */
  const request = async <T>(
    url: string,
    init: RequestInit,
    consume: (response: Response) => Promise<T>,
    timeoutMs = REQUEST_TIMEOUT_MS,
  ): Promise<T> => {
    const deadline = new AbortController()
    const signal = init.signal
      ? AbortSignal.any([init.signal, deadline.signal])
      : deadline.signal
    const timer = setTimeout(
      () => deadline.abort(new DOMException('请求超时，请重试', 'TimeoutError')),
      timeoutMs,
    )
    try {
      signal.throwIfAborted()
      return await consume(await fetchImpl(url, { ...init, signal }))
    } catch (error) {
      // 响应体读取可能将取消原因替换为 AbortError 或 HTTP 映射错误。
      signal.throwIfAborted()
      throw error
    } finally {
      clearTimeout(timer)
    }
  }

  /** 将会话 ID 编码为单个路径段；后缀是内部接口路径。 */
  const sessionUrl = (scope: RuntimeScope, suffix = ''): string =>
    `${config.apiBaseUrl}/v1/sessions/${encodeURIComponent(scope.sessionId)}${suffix}`

  /** 序列化读取接口所需的客户端和会话身份。 */
  const scopeQuery = (scope: RuntimeScope): string =>
    new URLSearchParams({
      app_id: config.appId,
      user_id: config.userId,
      agent_id: scope.agentId,
    }).toString()

  /** 逐段编码资产路径，同时保留资产 ID 中的斜杠分隔。 */
  const assetUrl = (assetId = ''): string => {
    const encoded = assetId.split('/').map(encodeURIComponent).join('/')
    return `${config.apiBaseUrl}/v1/assets${encoded ? `/${encoded}` : ''}`
  }

  /** 发送带身份的 JSON 命令；成功时无需响应体。 */
  const post = async (
    scope: RuntimeScope,
    suffix: string,
    body: Record<string, unknown>,
    signal?: AbortSignal,
  ): Promise<void> => {
    await request(
      sessionUrl(scope, suffix),
      {
        method: 'POST',
        headers: requestHeaders(config, { json: true }),
        body: JSON.stringify({
          app_id: config.appId,
          user_id: config.userId,
          agent_id: scope.agentId,
          ...body,
        }),
        signal,
      },
      ensureSuccess,
    )
  }

  /** 解析标题命令的权威 JSON 响应，不直接丢弃。 */
  async function updateTitle(
    scope: RuntimeScope,
    suffix: string,
    body: Record<string, unknown>,
    signal?: AbortSignal,
  ): Promise<SessionRecord> {
    const record = await request(
      sessionUrl(scope, suffix),
      {
        method: 'POST',
        headers: requestHeaders(config, { json: true }),
        signal,
        body: JSON.stringify({
          app_id: config.appId,
          user_id: config.userId,
          agent_id: scope.agentId,
          ...body,
        }),
      },
      readJson<SessionInfoResponse>,
      35_000,
    )
    return sessionRecord(record)
  }

  return {
    async sessions(cursor, signal) {
      const query = new URLSearchParams({
        app_id: config.appId,
        user_id: config.userId,
        limit: '20',
      })
      if (cursor) query.set('cursor', cursor)
      const page = await request(
        `${config.apiBaseUrl}/v1/sessions?${query}`,
        { headers: requestHeaders(config), signal },
        readJson<{ sessions: SessionInfoResponse[]; next_cursor: string | null }>,
      )
      return {
        sessions: page.sessions.map(sessionRecord),
        nextCursor: page.next_cursor,
      }
    },
    async renameSession(scope, title, signal) {
      return updateTitle(scope, '', { title }, signal)
    },
    async deleteSession(scope, signal) {
      await post(scope, '/delete', { hard_delete: false }, signal)
    },
    async generateSessionTitle(scope, signal) {
      return updateTitle(scope, '/title/generate', {}, signal)
    },
    async agents(signal) {
      const body = await request(
        `${config.apiBaseUrl}/v1/agents`,
        {
          headers: requestHeaders(config),
          signal,
        },
        readJson<unknown>,
      )
      return parseAgentCatalog(body)
    },
    async uploadAsset(file, signal) {
      const form = new FormData()
      form.append('file', file)
      return request(
        assetUrl(),
        {
          method: 'POST',
          headers: requestHeaders(config),
          body: form,
          signal,
        },
        readJson<AssetUploadResponse>,
        ASSET_TIMEOUT_MS,
      )
    },
    async readAsset(assetId, signal) {
      return request(
        assetUrl(assetId),
        {
          headers: requestHeaders(config),
          signal,
        },
        async (response) => {
          await ensureSuccess(response)
          return response.blob()
        },
        ASSET_TIMEOUT_MS,
      )
    },
    async submitMessage(scope, input, signal) {
      await post(
        scope,
        '/messages',
        {
          message_id: input.messageId,
          role: input.role,
          origin: input.origin,
          content: input.content,
          mode: input.mode,
        },
        signal,
      )
    },
    async resume(scope, resumeToken, input, signal) {
      await post(
        scope,
        '/resume',
        {
          resume_id: crypto.randomUUID(),
          resume_token: resumeToken,
          input,
        },
        signal,
      )
    },
    async cancel(scope, signal) {
      await post(scope, '/cancel', {}, signal)
    },
    async snapshot(scope, signal) {
      return request(
        `${sessionUrl(scope)}?${scopeQuery(scope)}`,
        {
          headers: requestHeaders(config),
          signal,
        },
        readJson<SessionSnapshot>,
      )
    },
    async messages(scope, signal) {
      const body = await request(
        `${sessionUrl(scope, '/messages')}?${scopeQuery(scope)}`,
        { headers: requestHeaders(config), signal },
        readJson<{ messages: PublicMessage[] }>,
      )
      return body.messages
    },
    async pendingInputs(scope, signal) {
      const body = await request(
        `${sessionUrl(scope, '/inputs/pending')}?${scopeQuery(scope)}`,
        { headers: requestHeaders(config), signal },
        readJson<{ inputs: PendingInputResponse[] }>,
      )
      return body.inputs.map((input) => ({
        messageId: input.message_id,
        role: input.role,
        origin: input.origin,
        content: input.content,
        blocks: input.blocks,
        mode: input.mode,
        createdAt: input.created_at,
      }))
    },
    async todos(scope, signal) {
      const body = await request(
        `${sessionUrl(scope, '/todos')}?${scopeQuery(scope)}`,
        { headers: requestHeaders(config), signal },
        readJson<{ todos: TodoItem[] }>,
      )
      return body.todos
    },
    async events(scope, cursor, signal) {
      const headers = requestHeaders(config)
      headers.set('Accept', 'text/event-stream')
      if (cursor) headers.set('Last-Event-ID', cursor)
      return request(
        `${sessionUrl(scope, '/events')}?${scopeQuery(scope)}`,
        { headers, signal },
        async (response) => {
          await ensureSuccess(response)
          const contentType = response.headers.get('content-type') ?? ''
          if (!contentType.toLowerCase().startsWith('text/event-stream')) {
            throw new RuntimeHttpError(
              response.status,
              'invalid_content_type',
              '服务端未返回 SSE 响应',
            )
          }
          if (response.body === null) {
            throw new RuntimeHttpError(
              response.status,
              'empty_stream',
              'SSE 响应没有正文',
            )
          }
          return response
        },
      )
    },
  }
}

/**
 * 已接受的待处理输入在驼峰字段映射前的传输结构。
 */
interface PendingInputResponse {
  message_id: string
  role: 'user' | 'system'
  origin: string
  content: string
  blocks: NonNullable<PendingInput['blocks']>
  mode: SubmissionMode
  created_at: string
}

/**
 * 已校验的目录传输条目；output_mode 映射到 AgentDefinition.outputMode。
 */
interface AgentCatalogItemResponse {
  id: string
  name: string
  description: string
  output_mode: OutputMode
}

/**
 * 校验目录封装并将服务端字段映射为前端定义。
 *
 * @param value - 目录接口返回的未受信任 JSON。
 * @returns 保持服务端顺序的 Agent 定义。
 * @throws Error 目录封装或任一 Agent 条目无效时抛出。
 */
function parseAgentCatalog(value: unknown): AgentDefinition[] {
  if (!isRecord(value) || !Array.isArray(value.agents)) {
    throw invalidAgentCatalog()
  }
  return value.agents.map((item) => {
    if (!isAgentCatalogItem(item)) throw invalidAgentCatalog()
    return {
      id: item.id,
      name: item.name,
      description: item.description,
      outputMode: item.output_mode,
    }
  })
}

/**
 * 启用 Agent 前检查目录必填字段和受支持的输出模式。
 */
function isAgentCatalogItem(value: unknown): value is AgentCatalogItemResponse {
  return (
    isRecord(value) &&
    typeof value.id === 'string' &&
    value.id.trim().length > 0 &&
    typeof value.name === 'string' &&
    value.name.trim().length > 0 &&
    typeof value.description === 'string' &&
    (value.output_mode === 'stream' || value.output_mode === 'complete')
  )
}

/**
 * 创建目录响应格式错误时显示给用户的错误。
 */
function invalidAgentCatalog(): Error {
  return new Error('服务端返回了无效的 Agent 目录')
}

/**
 * 为每次请求创建新的请求头并获取当前 Token。
 *
 * @param config - 客户端身份及可选的按请求获取 Bearer Token 的提供器。
 * @param options - 仅 JSON 请求体设置 json；FormData 需要浏览器生成的边界。
 * @returns Token 缺失或为空时不含 Authorization 字段的请求头。
 */
function requestHeaders(
  config: RuntimeClientConfig,
  options: { json?: boolean } = {},
): Headers {
  const headers = new Headers()
  if (options.json) headers.set('Content-Type', 'application/json')
  const token = config.tokenProvider?.()?.trim()
  if (token) headers.set('Authorization', `Bearer ${token}`)
  return headers
}

/**
 * 解析 JSON 响应前先拒绝 HTTP 失败。
 *
 * @typeParam T - 预期响应结构；此函数不校验该结构。
 * @param response - 响应体尚未读取的 Fetch 响应。
 * @returns 解析后断言为 T 的 JSON。
 * @throws RuntimeHttpError 状态码非成功时抛出；JSON 错误原样传播。
 */
async function readJson<T>(response: Response): Promise<T> {
  await ensureSuccess(response)
  return (await response.json()) as T
}

/**
 * 将非成功响应转换为结构化 RuntimeHttpError。
 *
 * 尽可能读取错误响应体，并向调用方提供数值型 Retry-After。
 * 成功响应保持未读取状态，供调用方自行处理响应体。
 */
async function ensureSuccess(response: Response): Promise<void> {
  if (response.ok) return
  const body = await safeErrorBody(response)
  const retryAfterHeader = response.headers.get('retry-after')
  const retryAfter = retryAfterHeader === null ? NaN : Number(retryAfterHeader)
  throw new RuntimeHttpError(
    response.status,
    stringField(body, 'code') ?? stringField(body, 'detail') ?? 'http_error',
    stringField(body, 'message') ?? `请求失败（HTTP ${response.status}）`,
    Number.isFinite(retryAfter) && retryAfter >= 0 ? retryAfter : undefined,
  )
}

/**
 * 尽可能读取 JSON 错误对象；无效、空值或非对象响应体视为 {}。
 */
async function safeErrorBody(response: Response): Promise<Record<string, unknown>> {
  try {
    const value: unknown = await response.json()
    return isRecord(value) ? value : {}
  } catch {
    return {}
  }
}

/**
 * 读取字符串字段，不强制转换意外的响应值。
 */
function stringField(
  value: Record<string, unknown>,
  field: string,
): string | undefined {
  return typeof value[field] === 'string' ? value[field] : undefined
}

/**
 * 将未知 JSON 值收窄为非 null、非数组的对象。
 */
function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}
