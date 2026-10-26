/**
 * 将公开事件投影为客户端消息和时间线状态。
 *
 * 导出的归约函数不修改输入状态。私有投影函数只修改 applyPublicEvent 创建的时间线副本。
 * 事件 ID 用于重放去重；SSE 游标是独立的传输检查点，不能与事件 ID 混淆。
 */

import type {
  BlockTimelineItem,
  CompactionTimelineItem,
  PendingInput,
  PublicEventEnvelope,
  PublicMessage,
  StreamTimeline,
  TerminalSummary,
  TimelineItem,
  TokenUsage,
  ToolTimelineItem,
} from '../domain/types'

/**
 * 按消息 ID 对齐的可见输入历史与已接受但未领取的输入。
 */
export interface MessageProjection {
  messages: PublicMessage[]
  pendingInputs: PendingInput[]
}

/**
 * 将已领取的输入消息从待处理列表移入可见历史。
 *
 * @param current - 现有消息和待处理输入数组，按只读处理。
 * @param event - 公开事件；无关类型及无效输入消息会被忽略。
 * @returns 无重复消息 ID 的更新数组；无变化时返回 current。
 */
export function applyMessageStarted(
  current: MessageProjection,
  event: PublicEventEnvelope,
): MessageProjection {
  if (event.type !== 'message.started') return current
  const message = publicMessageValue(event.payload.message)
  if (!message) return current
  const pendingInputs = current.pendingInputs.filter(
    (input) => input.messageId !== message.message_id,
  )
  if (current.messages.some((item) => item.message_id === message.message_id)) {
    if (pendingInputs.length === current.pendingInputs.length) return current
    return { messages: current.messages, pendingInputs }
  }
  return {
    messages: [...current.messages, message],
    pendingInputs,
  }
}

/**
 * 为新的运行投影创建相互独立的空数组。
 */
export function emptyTimeline(): StreamTimeline {
  return { items: [], appliedEventIds: [] }
}

/**
 * 应用一次公开事件并推进传输游标。
 *
 * @param current - 用于派生新状态的只读时间线。
 * @param event - 公开事件封装，其 event_id 是重放去重键。
 * @param cursor - 用于 Last-Event-ID 恢复的 SSE 帧 ID。
 * @returns 投影副本；事件已应用时返回原引用。
 * @remarks 未知事件类型仍记录 ID 和游标，但不新增条目。
 */
export function applyPublicEvent(
  current: StreamTimeline,
  event: PublicEventEnvelope,
  cursor: string,
): StreamTimeline {
  if (current.appliedEventIds.includes(event.event_id)) return current
  const next: StreamTimeline = {
    ...current,
    items: current.items.map((item) => ({ ...item })),
    appliedEventIds: [...current.appliedEventIds, event.event_id],
    cursor,
  }
  project(next, event)
  return next
}

/**
 * 将事件分发至 applyPublicEvent 拥有的可修改副本。
 */
function project(timeline: StreamTimeline, event: PublicEventEnvelope): void {
  if (event.type === 'message.started') {
    projectMessageBoundary(timeline, event)
  } else if (event.type.startsWith('message.block.')) {
    projectBlock(timeline, event)
  } else if (event.type.startsWith('tool.')) {
    projectTool(timeline, event)
  } else if (event.type.startsWith('context.compaction.')) {
    projectCompaction(timeline, event)
  } else if (event.type === 'run.suspended') {
    timeline.resumeToken = stringValue(event.payload.resume_token)
    timeline.resumeExpiresAt = stringValue(event.payload.expires_at)
    upsertStatus(timeline, event, '等待继续', 'pending')
  } else if (isTerminal(event.type)) {
    failRunningCompaction(timeline, event)
    timeline.terminal = terminalSummary(event)
  }
}

/**
 * 更新最近一次运行中的压缩，保留其键和开始时间戳。
 */
function projectCompaction(timeline: StreamTimeline, event: PublicEventEnvelope): void {
  const runningIndex = lastRunningCompactionIndex(timeline.items)
  const existing =
    runningIndex >= 0
      ? (timeline.items[runningIndex] as CompactionTimelineItem)
      : undefined
  const completed = event.type === 'context.compaction.completed'
  const item: CompactionTimelineItem = {
    kind: 'compaction',
    key: existing?.key ?? event.event_id,
    messageId: stringValue(event.payload.message_id) ?? existing?.messageId,
    status: completed ? 'completed' : 'running',
    action: compactionAction(event.payload.action) ?? existing?.action,
    beforeTokens: numberValue(event.payload.before_tokens) ?? existing?.beforeTokens,
    afterTokens: numberValue(event.payload.after_tokens) ?? existing?.afterTokens,
    durationMs: completed
      ? elapsedMilliseconds(existing?.createdAt ?? event.created_at, event.created_at)
      : undefined,
    createdAt: existing?.createdAt ?? event.created_at,
  }
  if (runningIndex >= 0) timeline.items[runningIndex] = item
  else timeline.items.push(item)
}

/**
 * 运行失败或取消时，将未完成的压缩标记为失败。
 */
function failRunningCompaction(
  timeline: StreamTimeline,
  event: PublicEventEnvelope,
): void {
  if (event.type !== 'run.failed' && event.type !== 'run.canceled') return
  const index = lastRunningCompactionIndex(timeline.items)
  if (index < 0) return
  const item = timeline.items[index] as CompactionTimelineItem
  timeline.items[index] = {
    ...item,
    status: 'failed',
    durationMs: elapsedMilliseconds(item.createdAt, event.created_at),
  }
}

/**
 * 查找最后一次进行中的压缩；不存在时返回 -1。
 */
function lastRunningCompactionIndex(items: TimelineItem[]): number {
  for (let index = items.length - 1; index >= 0; index -= 1) {
    const item = items[index]
    if (item?.kind === 'compaction' && item.status === 'running') return index
  }
  return -1
}

/**
 * 仅接受视图支持的公开压缩操作。
 */
function compactionAction(
  value: unknown,
): CompactionTimelineItem['action'] | undefined {
  return value === 'pruned' || value === 'summarized' ? value : undefined
}

/**
 * 为每条输入消息追加边界，使实时输出归入对应输入。
 */
function projectMessageBoundary(
  timeline: StreamTimeline,
  event: PublicEventEnvelope,
): void {
  const message = recordValue(event.payload.message)
  const messageId = message && stringValue(message.message_id)
  if (
    !messageId ||
    timeline.items.some(
      (item) => item.kind === 'message-boundary' && item.messageId === messageId,
    )
  ) {
    return
  }
  timeline.items.push({
    kind: 'message-boundary',
    key: messageId,
    messageId,
    createdAt: event.created_at,
  })
}

/**
 * 累积消息块增量，或用完成后的内容替换。
 *
 * 按内容块 ID 匹配，优先使用消息及索引键稳定对齐历史。
 * 文本原样拼接，不裁剪或规范化空白字符。
 */
function projectBlock(timeline: StreamTimeline, event: PublicEventEnvelope): void {
  const blockId = stringValue(event.payload.block_id)
  if (!blockId) return
  const existingIndex = timeline.items.findIndex(
    (item) =>
      item.kind === 'block' &&
      (item.blockId === blockId || (!item.blockId && item.key === blockId)),
  )
  const existing =
    existingIndex >= 0
      ? (timeline.items[existingIndex] as BlockTimelineItem)
      : undefined
  const delta = stringValue(event.payload.delta) ?? ''
  const completedContent = stringValue(event.payload.content)
  const messageId = stringValue(event.payload.message_id) ?? existing?.messageId
  const index = numberValue(event.payload.index) ?? existing?.index
  const block: BlockTimelineItem = {
    kind: 'block',
    key:
      messageId !== undefined && index !== undefined
        ? `${messageId}-${index}`
        : (existing?.key ?? blockId),
    blockId,
    messageId,
    index,
    blockType: stringValue(event.payload.block_type) ?? existing?.blockType ?? 'text',
    content: completedContent ?? `${existing?.content ?? ''}${delta}`,
    completed: event.type === 'message.block.completed',
    durationMs:
      event.type === 'message.block.completed'
        ? elapsedMilliseconds(existing?.createdAt ?? event.created_at, event.created_at)
        : existing?.durationMs,
    createdAt: existing?.createdAt ?? event.created_at,
  }
  if (existingIndex >= 0) timeline.items[existingIndex] = block
  else timeline.items.push(block)
}

/**
 * 按工具调用 ID 合并生命周期数据，保留此次事件未提供的旧字段。
 */
function projectTool(timeline: StreamTimeline, event: PublicEventEnvelope): void {
  const callId = stringValue(event.payload.tool_call_id)
  if (!callId) return
  const index = timeline.items.findIndex(
    (item) => item.kind === 'tool' && item.key === callId,
  )
  const existing = index >= 0 ? (timeline.items[index] as ToolTimelineItem) : undefined
  const status =
    event.type === 'tool.started'
      ? 'running'
      : event.type === 'tool.completed'
        ? 'completed'
        : 'failed'
  const tool: ToolTimelineItem = {
    kind: 'tool',
    key: callId,
    name: stringValue(event.payload.tool_name) ?? existing?.name ?? 'tool',
    status,
    arguments: recordValue(event.payload.arguments) ?? existing?.arguments,
    content: stringValue(event.payload.content) ?? existing?.content,
    durationMs: numberValue(event.payload.duration_ms) ?? existing?.durationMs,
    isError: booleanValue(event.payload.is_error) ?? existing?.isError,
    truncated: booleanValue(event.payload.truncated) ?? existing?.truncated,
    reasonCode: stringValue(event.payload.reason_code) ?? existing?.reasonCode,
    createdAt: existing?.createdAt ?? event.created_at,
  }
  if (index >= 0) timeline.items[index] = tool
  else timeline.items.push(tool)
}

/**
 * 替换运行状态行，或按事件 ID 追加独立的非运行状态。
 */
function upsertStatus(
  timeline: StreamTimeline,
  event: PublicEventEnvelope,
  label: string,
  tone: 'neutral' | 'active' | 'success' | 'danger' | 'pending',
): void {
  const key = event.type.startsWith('run.') ? 'run-status' : event.event_id
  const item: TimelineItem = {
    kind: 'status',
    key,
    label,
    tone,
    createdAt: event.created_at,
  }
  const index = timeline.items.findIndex(
    (candidate) => candidate.kind === 'status' && candidate.key === key,
  )
  if (index >= 0) timeline.items[index] = item
  else timeline.items.push(item)
}

/**
 * 读取终态及可选的用量、耗时和错误元数据。
 *
 * 嵌套元数据仅对对象形状做类型断言，不进行深层校验。
 */
function terminalSummary(event: PublicEventEnvelope): TerminalSummary {
  const rawStatus = stringValue(event.payload.status)
  const status =
    rawStatus === 'failed' || rawStatus === 'canceled' ? rawStatus : 'succeeded'
  return {
    status,
    usage: recordValue(event.payload.usage) as TokenUsage | undefined,
    timing: recordValue(event.payload.timing) as TerminalSummary['timing'],
    error: recordValue(event.payload.error),
  }
}

/**
 * 识别完成、失败和取消事件；挂起不属于终态。
 */
function isTerminal(type: string): boolean {
  return ['run.completed', 'run.failed', 'run.canceled'].includes(type)
}

/**
 * 字符串原样返回；其他载荷类型返回 undefined。
 */
function stringValue(value: unknown): string | undefined {
  return typeof value === 'string' ? value : undefined
}

/**
 * 返回数值载荷，不强制转换或校验范围。
 */
function numberValue(value: unknown): number | undefined {
  return typeof value === 'number' ? value : undefined
}

/**
 * 计算非负时长；时间戳无效时返回 undefined。
 */
function elapsedMilliseconds(start: string, end: string): number | undefined {
  const startTime = Date.parse(start)
  const endTime = Date.parse(end)
  if (Number.isNaN(startTime) || Number.isNaN(endTime)) return undefined
  return Math.max(0, endTime - startTime)
}

/**
 * 仅返回布尔值，不强制转换其他真假值。
 */
function booleanValue(value: unknown): boolean | undefined {
  return typeof value === 'boolean' ? value : undefined
}

/**
 * 返回对象形状的载荷数据，拒绝 null 和数组。
 */
function recordValue(value: unknown): Record<string, unknown> | undefined {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : undefined
}

/**
 * 校验 message.started 投影所需的输入消息字段。
 *
 * 仅接受 is_error=false 的 user 或 system 消息。只检查 blocks 是否为数组，
 * 不做深层校验；必填字段无效时忽略该事件。
 */
function publicMessageValue(value: unknown): PublicMessage | undefined {
  const message = recordValue(value)
  if (
    !message ||
    typeof message.message_id !== 'string' ||
    (message.role !== 'user' && message.role !== 'system') ||
    typeof message.origin !== 'string' ||
    typeof message.content !== 'string' ||
    typeof message.created_at !== 'string' ||
    message.is_error !== false
  ) {
    return undefined
  }
  return {
    message_id: message.message_id,
    role: message.role,
    origin: message.origin,
    content: message.content,
    blocks: Array.isArray(message.blocks)
      ? (message.blocks as PublicMessage['blocks'])
      : undefined,
    created_at: message.created_at,
    is_error: false,
  }
}
