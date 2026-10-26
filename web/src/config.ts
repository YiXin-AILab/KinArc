/**
 * 前端配置与服务端提供的 Agent 目录类型。
 *
 * 只有连接身份来自构建时变量；Agent 定义和输出模式由后端加载，前端不重复配置。
 */

/**
 * 服务端声明的观察模式：SSE 流或轮询快照直至完成。
 */
export type OutputMode = 'stream' | 'complete'

/**
 * 已校验的目录条目，用于选择 Agent 和观察策略。
 */
export interface AgentDefinition {
  id: string
  name: string
  description: string
  outputMode: OutputMode
}

/**
 * 前端连接身份；apiBaseUrl 为空时使用同源请求和开发代理。
 */
export interface RuntimeConfig {
  apiBaseUrl: string
  appId: string
  userId: string
}

/**
 * 构建时字符串变量；未定义的条目由 loadRuntimeConfig 使用默认值。
 */
type Environment = Record<string, string | undefined>

/**
 * 读取前端连接配置，无需环境文件。
 *
 * @param environment - 构建时变量；缺失的身份字段使用本地默认值。
 * @returns 规范化的 API 基础 URL、应用 ID 和用户 ID。
 */
export function loadRuntimeConfig(environment: Environment): RuntimeConfig {
  return {
    apiBaseUrl: trimTrailingSlash(environment.VITE_API_BASE_URL ?? ''),
    appId: nonEmpty(environment.VITE_APP_ID, 'agent-web'),
    userId: nonEmpty(environment.VITE_USER_ID, 'local-user'),
  }
}

/**
 * 去除配置值首尾空白；缺失或空值时使用默认值。
 */
function nonEmpty(value: string | undefined, fallback: string): string {
  return value?.trim() || fallback
}

/**
 * 拼接 API 路径前去除首尾空白及末尾斜杠。
 */
function trimTrailingSlash(value: string): string {
  return value.trim().replace(/\/+$/, '')
}
