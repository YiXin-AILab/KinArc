/**
 * 从持久化消息和临时流事件构建展示条目。
 *
 * 导出的组装函数不修改输入。内部追加函数只修改新建的条目和内容部分。
 * 稳定的消息及内容块键使实时输出被历史记录替换时组件身份保持不变。
 * 文本空白字符原样保留。
 */

import type {
  PublicMessage,
  PublicMessageBlock,
  PublicUserMessageBlock,
  StreamTimeline,
  TimelineItem,
} from '../domain/types'

/**
 * 可渲染的 Agent 活动，实时内容替换为历史记录时保持稳定键。
 *
 * 内容部分是新组装的视图数据；原始消息和时间线条目保持只读。
 */
export type AgentPart =
  | {
      kind: 'text'
      key: string
      text: string
      isLive: boolean
      completed: boolean
    }
  | {
      kind: 'thinking'
      key: string
      content: string
      isLive: boolean
      completed: boolean
      durationMs?: number
    }
  | {
      kind: 'tool'
      key: string
      toolCallId?: string
      name: string
      content?: string
      isError: boolean
      status: 'running' | 'completed' | 'failed'
      arguments?: Record<string, unknown>
      durationMs?: number
      reasonCode?: string
    }
  | {
      kind: 'compaction'
      key: string
      messageId?: string
      status: 'running' | 'completed' | 'failed'
      action?: 'pruned' | 'summarized'
      beforeTokens?: number
      afterTokens?: number
      durationMs?: number
    }
/**
 * 一条输入消息，或输入边界之后连续的 Agent 活动。
 */
export type ConversationEntry =
  | { kind: 'user'; key: string; message: PublicMessage }
  | {
      kind: 'agent'
      key: string
      createdAt: string
      parts: AgentPart[]
      isError: boolean
      isLive: boolean
    }

/**
 * 选取尚未由持久化历史表示的临时条目。
 *
 * @param messages - 用于判断结构化输出覆盖情况的权威历史。
 * @param timeline - 当前事件投影，可能包含终态摘要。
 * @returns 筛选后的数组；保留的条目与输入共享，必须保持只读。
 * @remarks 状态行不显示；已完成的压缩通过消息 ID 匹配。
 */
export function visibleConversationEvents(
  messages: PublicMessage[],
  timeline: StreamTimeline,
): TimelineItem[] {
  const hasStructuredHistory = messages.some((message) =>
    message.blocks?.some((block) => block.type !== 'text'),
  )
  return timeline.items.filter((item) => {
    if (item.kind === 'status') return false
    if (item.kind === 'compaction') {
      return !(
        item.status === 'completed' &&
        item.messageId !== undefined &&
        messages.some(
          (message) =>
            message.message_id === item.messageId &&
            message.blocks?.some((block) => block.type === 'compaction'),
        )
      )
    }
    if (!timeline.terminal) return true
    if (hasStructuredHistory) return false
    return !(item.kind === 'block' && item.blockType === 'text')
  })
}

/**
 * 组装可渲染的对话条目，不修改源状态。
 *
 * @param messages - 按展示顺序排列的持久化和乐观消息。
 * @param timeline - 当前投影的临时事件和耗时元数据。
 * @returns 具有稳定键的新条目和内容部分；用户条目引用原始消息。
 * @remarks 文本及空行均按接收内容原样保留。
 */
export function buildConversationEntries(
  messages: PublicMessage[],
  timeline: StreamTimeline,
): ConversationEntry[] {
  const entries = mergeLiveTimeline(
    groupMessages(messages),
    visibleConversationEvents(messages, timeline),
  )
  applyTimelineMetadata(entries, timeline.items)
  return entries
}

/**
 * 选取用户可见的文本和媒体块，缺失时使用旧版消息 content。
 *
 * @param message - 作为输入内容渲染的用户或系统消息。
 * @returns 新的内容块列表；已有块仍是共享的只读引用。
 */
export function userBlocks(message: PublicMessage): PublicUserMessageBlock[] {
  const blocks = (message.blocks ?? []).filter(
    (block): block is PublicUserMessageBlock =>
      block.type === 'text' ||
      block.type === 'image' ||
      block.type === 'audio' ||
      block.type === 'video' ||
      block.type === 'document',
  )
  if (blocks.length > 0) return blocks
  return message.content ? [{ type: 'text', text: message.content }] : []
}

/**
 * 将每条输入消息之后连续的 assistant 和 tool 消息分组。
 *
 * 压缩块即使属于 system 消息，也归入 Agent 活动。
 * Agent 条目重新创建，输入条目保留原始消息引用。
 */
function groupMessages(messages: PublicMessage[]): ConversationEntry[] {
  const entries: ConversationEntry[] = []
  let currentAgent: Extract<ConversationEntry, { kind: 'agent' }> | undefined
  let precedingUserId = 'root'
  for (const message of messages) {
    const isCompaction = message.blocks?.some((block) => block.type === 'compaction')
    if (!isCompaction && (message.role === 'user' || message.role === 'system')) {
      currentAgent = undefined
      precedingUserId = message.message_id
      entries.push({ kind: 'user', key: message.message_id, message })
      continue
    }
    if (!currentAgent) {
      currentAgent = {
        kind: 'agent',
        key: agentEntryKey(precedingUserId),
        createdAt: message.created_at,
        parts: [],
        isError: false,
        isLive: false,
      }
      entries.push(currentAgent)
    }
    currentAgent.isError ||= message.is_error
    appendMessageParts(currentAgent.parts, message)
  }
  return entries
}

/**
 * 将实时片段关联到对应的输入消息边界。
 *
 * 修改本地拥有的条目数组。没有显式边界时追加到最后一条对话条目；
 * 否则按 message.started 标记拆分事件。
 */
function mergeLiveTimeline(
  entries: ConversationEntry[],
  liveItems: TimelineItem[],
): ConversationEntry[] {
  if (liveItems.length === 0) return entries
  if (!liveItems.some((item) => item.kind === 'message-boundary')) {
    appendLiveSegment(entries, undefined, liveItems)
    return entries
  }
  let activeMessageId: string | undefined
  let segment: TimelineItem[] = []
  for (const item of liveItems) {
    if (item.kind !== 'message-boundary') {
      segment.push(item)
      continue
    }
    appendLiveSegment(
      entries,
      activeMessageId ?? precedingUserId(entries, item.messageId),
      segment,
    )
    activeMessageId = item.messageId
    segment = []
  }
  appendLiveSegment(entries, activeMessageId, segment)
  return entries
}

/**
 * 查找实时边界之前的输入消息；未知时返回 undefined。
 */
function precedingUserId(
  entries: ConversationEntry[],
  messageId: string,
): string | undefined {
  const boundaryIndex = entries.findIndex(
    (entry) => entry.kind === 'user' && entry.message.message_id === messageId,
  )
  for (let index = boundaryIndex - 1; index >= 0; index -= 1) {
    const entry = entries[index]
    if (entry?.kind === 'user') return entry.message.message_id
  }
  return undefined
}

/**
 * 插入或复用与一条输入消息关联的 Agent 条目。
 *
 * @param entries - 本地拥有且可修改的展示条目。
 * @param messageId - 边界消息 ID；undefined 表示关联到最后一条。
 * @param items - 待追加的只读实时条目；空片段会被忽略。
 */
function appendLiveSegment(
  entries: ConversationEntry[],
  messageId: string | undefined,
  items: TimelineItem[],
): void {
  if (items.length === 0) return
  const targetIndex = messageId
    ? entries.findIndex(
        (entry) => entry.kind === 'user' && entry.message.message_id === messageId,
      )
    : entries.length - 1
  const lastEntry = entries[targetIndex]
  let agentEntry: Extract<ConversationEntry, { kind: 'agent' }>
  const followingEntry = entries[targetIndex + 1]
  if (followingEntry?.kind === 'agent') {
    agentEntry = followingEntry
  } else if (!messageId && lastEntry?.kind === 'agent') {
    agentEntry = lastEntry
  } else {
    agentEntry = {
      kind: 'agent',
      key: agentEntryKey(
        messageId ??
          (lastEntry?.kind === 'user' ? lastEntry.message.message_id : 'root'),
      ),
      createdAt: items[0]!.createdAt,
      parts: [],
      isError: false,
      isLive: true,
    }
    entries.splice(targetIndex + 1, 0, agentEntry)
  }
  agentEntry.isLive = true
  appendLiveParts(agentEntry.parts, items)
}

/**
 * 根据前一条输入消息 ID 派生稳定的 Agent 条目键。
 */
function agentEntryKey(precedingUserId: string): string {
  return `agent-after-${precedingUserId}`
}

/**
 * 将实时条目转换为新的展示内容部分，并按事件顺序追加。
 *
 * 工具调用块跳过，因为工具生命周期条目已表示它们。
 * 状态与边界标记影响分组，但不会渲染为消息内容。
 */
function appendLiveParts(parts: AgentPart[], items: TimelineItem[]): void {
  for (const item of items) {
    if (item.kind === 'status' || item.kind === 'message-boundary') continue
    if (item.kind === 'compaction') {
      parts.push({
        kind: 'compaction',
        key: `compaction-${item.key}`,
        messageId: item.messageId,
        status: item.status,
        action: item.action,
        beforeTokens: item.beforeTokens,
        afterTokens: item.afterTokens,
        durationMs: item.durationMs,
      })
      continue
    }
    if (item.kind === 'block' && item.blockType === 'tool_call') continue
    if (item.kind === 'tool') {
      parts.push({
        kind: 'tool',
        key: `tool-${item.key}`,
        toolCallId: item.key,
        name: item.name,
        content: item.content,
        isError: item.status === 'failed' || item.isError === true,
        status: item.status,
        arguments: item.arguments,
        durationMs: item.durationMs,
        reasonCode: item.reasonCode,
      })
    } else if (item.blockType === 'thinking') {
      parts.push({
        kind: 'thinking',
        key: item.key,
        content: item.content,
        isLive: true,
        completed: item.completed,
        durationMs: item.durationMs,
      })
    } else {
      parts.push({
        kind: 'text',
        key: item.key,
        text: item.content,
        isLive: true,
        completed: item.completed,
      })
    }
  }
}

/**
 * 为本地组装的内容部分补全缺失的耗时和压缩计数。
 *
 * 使用内容块和工具键、压缩消息 ID 匹配；已有历史元数据优先。
 * 源时间线条目只读，不会被修改。
 */
function applyTimelineMetadata(
  entries: ConversationEntry[],
  items: TimelineItem[],
): void {
  const durations = new Map<string, number>()
  const compactions = new Map<string, Extract<TimelineItem, { kind: 'compaction' }>>()
  for (const item of items) {
    if (item.kind === 'compaction' && item.messageId !== undefined) {
      compactions.set(item.messageId, item)
    }
    if (
      item.kind === 'status' ||
      item.kind === 'message-boundary' ||
      item.durationMs === undefined
    ) {
      continue
    }
    durations.set(item.kind === 'tool' ? `tool-${item.key}` : item.key, item.durationMs)
  }
  for (const entry of entries) {
    if (entry.kind !== 'agent') continue
    for (const part of entry.parts) {
      if (part.kind === 'text') continue
      if (part.kind === 'compaction' && part.messageId !== undefined) {
        const metadata = compactions.get(part.messageId)
        part.beforeTokens ??= metadata?.beforeTokens
        part.afterTokens ??= metadata?.afterTokens
        part.durationMs ??= metadata?.durationMs
      }
      part.durationMs ??= durations.get(part.key)
    }
  }
}

/**
 * 追加结构化历史内容块；没有块时使用 content 字段。
 */
function appendMessageParts(parts: AgentPart[], message: PublicMessage): void {
  if (!message.blocks?.length) {
    appendLegacyPart(parts, message)
    return
  }
  message.blocks.forEach((block, index) =>
    appendBlock(parts, block, `${message.message_id}-${index}`, message.message_id),
  )
}

/**
 * 将仅含 content 的历史消息渲染为已完成的文本或工具部分。
 *
 * 空字符串不会产生内容部分；仅含空白字符的内容仍会保留。
 */
function appendLegacyPart(parts: AgentPart[], message: PublicMessage): void {
  if (!message.content) return
  if (message.role === 'tool') {
    parts.push({
      kind: 'tool',
      key: message.message_id,
      name: '工具',
      content: message.content,
      isError: message.is_error,
      status: message.is_error ? 'failed' : 'completed',
    })
  } else {
    parts.push({
      kind: 'text',
      key: message.message_id,
      text: message.content,
      isLive: false,
      completed: true,
    })
  }
}

/**
 * 追加一个已存储的内容块，或将工具结果合并到之前的调用。
 *
 * @param parts - 当前 Agent 条目本地拥有且可修改的内容部分。
 * @param block - 只读的已存储消息块。
 * @param key - 非工具部分的稳定消息及内容块索引键。
 * @param messageId - 用于匹配压缩元数据的父消息身份。
 * @remarks 工具 ID 优先于名称匹配；媒体块不属于 Agent 内容部分。
 */
function appendBlock(
  parts: AgentPart[],
  block: PublicMessageBlock,
  key: string,
  messageId: string,
): void {
  if (block.type === 'text') {
    parts.push({
      kind: 'text',
      key,
      text: block.text,
      isLive: false,
      completed: true,
    })
  } else if (block.type === 'compaction') {
    parts.push({
      kind: 'compaction',
      key,
      messageId,
      status: 'completed',
    })
  } else if (block.type === 'thinking') {
    parts.push({
      kind: 'thinking',
      key,
      content: block.thinking,
      isLive: false,
      completed: true,
    })
  } else if (block.type === 'tool_call') {
    parts.push({
      kind: 'tool',
      key: `tool-${block.tool_call_id ?? key}`,
      toolCallId: block.tool_call_id,
      name: block.tool_name,
      isError: false,
      status: 'running',
    })
  } else if (
    block.type === 'image' ||
    block.type === 'audio' ||
    block.type === 'video' ||
    block.type === 'document'
  ) {
    return
  } else {
    const pending = parts.find(
      (part) =>
        part.kind === 'tool' &&
        (block.tool_call_id
          ? part.toolCallId === block.tool_call_id
          : part.name === block.tool_name) &&
        part.content === undefined,
    )
    if (pending?.kind === 'tool') {
      pending.content = block.content
      pending.isError = block.is_error
      pending.status = block.is_error ? 'failed' : 'completed'
    } else {
      parts.push({
        kind: 'tool',
        key: `tool-${block.tool_call_id ?? key}`,
        toolCallId: block.tool_call_id,
        name: block.tool_name,
        content: block.content,
        isError: block.is_error,
        status: block.is_error ? 'failed' : 'completed',
      })
    }
  }
}
