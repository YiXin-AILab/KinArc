/**
 * Runtime API 客户端共用的 HTTP 传输层。
 *
 * 统一管理请求超时、鉴权头、HTTP 错误映射和响应读取，客户端只需提供 URL 和请求数据。
 * 普通请求的超时覆盖连接与响应体读取；openStream 仅限制连接建立时间，
 * 连接建立后的流不设总超时，仍可由调用方取消。
 */

import { RuntimeHttpError } from './client'

/** 普通请求的默认超时，单位为毫秒。 */
export const DEFAULT_REQUEST_TIMEOUT_MS = 30_000

/**
 * 传输层配置；每次请求重新调用 tokenProvider，支持更新 Token。
 */
export interface HttpTransportConfig {
  tokenProvider?: () => string | null
}

/**
 * JSON 请求与流连接共用的单次请求选项。
 */
export interface HttpRequestOptions {
  method?: string
  /** 序列化为 JSON 的请求体，设置对应 Content-Type；与 formData 互斥。 */
  body?: unknown
  /** 表单请求体，由浏览器生成 Content-Type 的 boundary。 */
  formData?: FormData
  /** 在鉴权头之后合并的附加请求头，例如 SSE 重放使用的 Last-Event-ID。 */
  headers?: Record<string, string>
  /** 调用方的取消信号，与请求超时信号合并。 */
  signal?: AbortSignal
  /** 超时时间，单位为毫秒；默认使用 DEFAULT_REQUEST_TIMEOUT_MS。 */
  timeoutMs?: number
}

/**
 * Runtime API 客户端共用的请求接口。
 */
export interface HttpTransport {
  /**
   * 在有限超时内发送 JSON 请求并读取响应。
   *
   * @param url - 已构造的完整请求 URL。
   * @param options - 请求方法、请求体、附加请求头、取消信号及超时时间。
   * @returns 解析后的 JSON；成功响应体为空时返回 undefined，例如无正文的 201/204。
   * @throws RuntimeHttpError 响应状态非成功时抛出；超时或取消时抛出取消原因，
   * JSON 解析错误原样传播。
   */
  requestJson<T>(url: string, options?: HttpRequestOptions): Promise<T>
  /**
   * 打开流式响应，仅对连接建立设置超时。
   *
   * 返回的响应体不设总超时；调用方的取消信号仍然有效，
   * 响应体的读取和关闭由调用方负责。
   *
   * @throws RuntimeHttpError 响应状态非成功时抛出；未在超时前收到响应头时
   * 抛出 TimeoutError。
   */
  openStream(url: string, options?: HttpRequestOptions): Promise<Response>
}

/**
 * 创建统一管理 fetch、超时和鉴权的传输层。
 *
 * @param config - 配置，可提供按请求获取 Bearer Token 的函数。
 * @param fetchImpl - fetch 实现，可在传输层合约测试中注入。
 * @returns 请求方法；取消、网络错误和解析错误均向调用方传播。
 */
export function createHttpTransport(
  config: HttpTransportConfig,
  fetchImpl: typeof fetch = fetch,
): HttpTransport {
  /** 用同一超时覆盖请求与响应读取，并保留调用方的取消能力。 */
  const perform = async <T>(
    url: string,
    options: HttpRequestOptions,
    consume: (response: Response) => Promise<T>,
  ): Promise<T> => {
    const deadline = new AbortController()
    const signal = options.signal
      ? AbortSignal.any([options.signal, deadline.signal])
      : deadline.signal
    const timer = setTimeout(
      () => deadline.abort(new DOMException('请求超时，请重试', 'TimeoutError')),
      options.timeoutMs ?? DEFAULT_REQUEST_TIMEOUT_MS,
    )
    try {
      signal.throwIfAborted()
      return await consume(await fetchImpl(url, requestInit(options, config, signal)))
    } catch (error) {
      // 响应体读取可能将取消原因替换为 AbortError 或 HTTP 错误，优先恢复原始取消原因。
      signal.throwIfAborted()
      throw error
    } finally {
      clearTimeout(timer)
    }
  }

  return {
    requestJson<T>(url: string, options: HttpRequestOptions = {}): Promise<T> {
      return perform(url, options, readJson<T>)
    },
    openStream(url: string, options: HttpRequestOptions = {}): Promise<Response> {
      // 收到响应头后清除超时定时器，后续流读取仍受调用方取消信号控制。
      return perform(url, options, async (response) => {
        await ensureSuccess(response)
        return response
      })
    },
  }
}

/**
 * 为每次请求创建独立请求头，并使用当前 Token 构造 RequestInit。
 */
function requestInit(
  options: HttpRequestOptions,
  config: HttpTransportConfig,
  signal: AbortSignal,
): RequestInit {
  const headers = new Headers()
  if (options.body !== undefined) headers.set('Content-Type', 'application/json')
  const token = config.tokenProvider?.()?.trim()
  if (token) headers.set('Authorization', `Bearer ${token}`)
  for (const [name, value] of Object.entries(options.headers ?? {})) {
    headers.set(name, value)
  }
  return {
    method: options.method ?? 'GET',
    headers,
    body:
      options.formData ??
      (options.body === undefined ? undefined : JSON.stringify(options.body)),
    signal,
  }
}

/**
 * 解析 JSON 前先检查 HTTP 状态；响应体为空时返回 undefined。
 */
async function readJson<T>(response: Response): Promise<T> {
  await ensureSuccess(response)
  const text = await response.text()
  return (text ? JSON.parse(text) : undefined) as T
}

/**
 * 将非成功响应转换为结构化的 RuntimeHttpError。
 *
 * 尝试读取错误响应体，并向调用方传递有效的数值 Retry-After。
 * 成功响应体保持未读取，由调用方处理。
 */
async function ensureSuccess(response: Response): Promise<void> {
  if (response.ok) return
  const body = await safeErrorBody(response)
  const retryAfterHeader = response.headers.get('retry-after')
  const retryAfter = retryAfterHeader === null ? NaN : Number(retryAfterHeader)
  throw new RuntimeHttpError(
    response.status,
    stringField(body, 'code') ?? stringField(body, 'detail') ?? 'http_error',
    stringField(body, 'message') ??
      stringField(body, 'detail') ??
      `请求失败（HTTP ${response.status}）`,
    Number.isFinite(retryAfter) && retryAfter >= 0 ? retryAfter : undefined,
  )
}

/**
 * 尝试读取 JSON 错误对象；响应体为空、格式无效或不是对象时返回 {}。
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
 * 读取字符串字段，不对其他类型的响应值进行强制转换。
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
