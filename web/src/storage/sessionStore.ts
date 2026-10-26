/**
 * 用于流恢复检查点的浏览器存储。
 *
 * 工作台注入 sessionStorage，使恢复范围限于当前标签页。无效 JSON 会被忽略，
 * 但 Storage 访问错误（包括配额或安全错误）会传播给调用方。
 */

import type { SessionCheckpoint } from '../domain/types'

const CHECKPOINT_PREFIX = 'agentsystem.checkpoint.'

/**
 * 以会话 ID 为键的同步游标和投影操作；Storage 失败会传播。
 */
export interface CheckpointStore {
  /** 返回结构有效的检查点；存储值缺失或无效时返回 null。 */
  load(sessionId: string): SessionCheckpoint | null
  /** 序列化检查点；配额及存储访问错误会传播。 */
  save(sessionId: string, checkpoint: SessionCheckpoint): void
  /** 仅移除指定会话的恢复检查点。 */
  clear(sessionId: string): void
}

/**
 * 创建保存事件游标和可选重放投影的会话存储。
 *
 * @param storage - 通常为延迟获取 sessionStorage 的函数，使恢复限于当前标签页。
 * @returns 同步检查点操作；缺失或无效值读取为 null。
 * @throws Storage 读写和移除错误不会被吞掉。
 */
export function createCheckpointStore(
  storage: Storage | (() => Storage),
): CheckpointStore {
  /** 为检查点键添加命名空间，避免与会话摘要存储混用。 */
  const key = (sessionId: string): string => `${CHECKPOINT_PREFIX}${sessionId}`
  return {
    load(sessionId) {
      return parseCheckpoint(resolveStorage(storage).getItem(key(sessionId)))
    },
    save(sessionId, checkpoint) {
      resolveStorage(storage).setItem(key(sessionId), JSON.stringify(checkpoint))
    },
    clear(sessionId) {
      resolveStorage(storage).removeItem(key(sessionId))
    },
  }
}

/** 在每次操作中获取浏览器 Storage，使访问错误传给调用方。 */
function resolveStorage(storage: Storage | (() => Storage)): Storage {
  return typeof storage === 'function' ? storage() : storage
}

/**
 * 在损坏的恢复状态进入归约器或渲染器前将其拒绝。
 */
function parseCheckpoint(raw: string | null): SessionCheckpoint | null {
  const value = safeJson(raw)
  if (!isRecord(value) || typeof value.cursor !== 'string') return null
  if (
    !Array.isArray(value.appliedEventIds) ||
    !value.appliedEventIds.every((item) => typeof item === 'string')
  ) {
    return null
  }
  if (value.timeline !== undefined && !isTimeline(value.timeline)) return null
  return value as unknown as SessionCheckpoint
}

/** 校验投影元数据及各类受支持的时间线条目。 */
function isTimeline(value: unknown): boolean {
  return (
    isRecord(value) &&
    Array.isArray(value.items) &&
    value.items.every(isTimelineItem) &&
    Array.isArray(value.appliedEventIds) &&
    value.appliedEventIds.every((id) => typeof id === 'string') &&
    optionalFields(value, ['cursor', 'resumeToken', 'resumeExpiresAt'], 'string') &&
    (value.terminal === undefined || isTerminal(value.terminal))
  )
}

/** 校验对话组装和活动渲染使用的字段。 */
function isTimelineItem(value: unknown): boolean {
  if (
    !isRecord(value) ||
    typeof value.key !== 'string' ||
    typeof value.createdAt !== 'string'
  )
    return false
  switch (value.kind) {
    case 'status':
      return (
        typeof value.label === 'string' &&
        typeof value.tone === 'string' &&
        ['neutral', 'active', 'success', 'danger', 'pending'].includes(value.tone) &&
        optionalFields(value, ['detail'], 'string')
      )
    case 'block':
      return (
        typeof value.blockType === 'string' &&
        typeof value.content === 'string' &&
        typeof value.completed === 'boolean' &&
        optionalFields(value, ['blockId', 'messageId'], 'string') &&
        optionalFields(value, ['index', 'durationMs'], 'number')
      )
    case 'tool':
      return (
        typeof value.name === 'string' &&
        isActivityStatus(value.status) &&
        (value.arguments === undefined || isRecord(value.arguments)) &&
        optionalFields(value, ['content', 'reasonCode'], 'string') &&
        optionalFields(value, ['durationMs'], 'number') &&
        optionalFields(value, ['isError', 'truncated'], 'boolean')
      )
    case 'compaction':
      return (
        isActivityStatus(value.status) &&
        (value.action === undefined ||
          value.action === 'pruned' ||
          value.action === 'summarized') &&
        optionalFields(value, ['messageId'], 'string') &&
        optionalFields(value, ['beforeTokens', 'afterTokens', 'durationMs'], 'number')
      )
    case 'message-boundary':
      return typeof value.messageId === 'string'
    default:
      return false
  }
}

/** 仅接受工具和压缩条目支持的活动状态。 */
function isActivityStatus(value: unknown): boolean {
  return value === 'running' || value === 'completed' || value === 'failed'
}

/** 校验嵌套的终态计数、耗时和用户可见错误。 */
function isTerminal(value: unknown): boolean {
  if (
    !isRecord(value) ||
    typeof value.status !== 'string' ||
    !['succeeded', 'failed', 'canceled'].includes(value.status)
  )
    return false
  if (value.usage !== undefined) {
    const usage = value.usage
    if (
      !isRecord(usage) ||
      !['input_tokens', 'output_tokens', 'total_tokens'].every(
        (key) => typeof usage[key] === 'number' && Number.isFinite(usage[key]),
      ) ||
      !optionalFields(usage, ['accounting'], 'string') ||
      !['reasoning_tokens', 'cache_read_tokens', 'cache_write_tokens'].every(
        (key) =>
          usage[key] == null ||
          (typeof usage[key] === 'number' && Number.isFinite(usage[key])),
      )
    )
      return false
  }
  if (value.timing !== undefined) {
    const timing = value.timing
    if (
      !isRecord(timing) ||
      !['elapsed_ms', 'active_ms'].every(
        (key) => typeof timing[key] === 'number' && Number.isFinite(timing[key]),
      ) ||
      !optionalFields(timing, ['started_at', 'ended_at'], 'string')
    )
      return false
  }
  return (
    value.error == null ||
    (isRecord(value.error) &&
      optionalFields(value.error, ['code', 'message'], 'string') &&
      optionalFields(value.error, ['retryable'], 'boolean'))
  )
}

/** 检查可选的基本类型字段，不接受 null 或非有限数值。 */
function optionalFields(
  value: Record<string, unknown>,
  keys: string[],
  type: 'string' | 'number' | 'boolean',
): boolean {
  return keys.every(
    (key) =>
      value[key] === undefined ||
      (typeof value[key] === type &&
        (type !== 'number' || Number.isFinite(value[key]))),
  )
}

/**
 * JSON 缺失或无效时返回 null；不捕获 Storage 访问错误。
 */
function safeJson(raw: string | null): unknown {
  if (raw === null) return null
  try {
    return JSON.parse(raw)
  } catch {
    return null
  }
}

/**
 * 接受普通 JSON 对象，拒绝 null 和数组。
 */
function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}
