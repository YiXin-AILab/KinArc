/**
 * 共享传输协议与客户端对话状态。
 *
 * 下划线命名字段对应 Runtime 公开 API，驼峰命名记录表示本地视图状态。
 * 会话快照决定执行状态，消息历史是持久化内容的权威来源，时间线保存临时事件投影。
 */

/**
 * 公开执行状态；pending 表示等待恢复的挂起运行，而非排队输入。
 */
export type SessionStatus =
  | 'idle'
  | 'running'
  | 'pending'
  | 'succeeded'
  | 'failed'
  | 'canceled'

/**
 * 输入调度意图：queue 等待运行边界，steer 可在工具循环中被领取。
 */
export type SubmissionMode = 'queue' | 'steer'

/**
 * 新提交输入允许的角色；assistant 和 tool 消息由执行过程产生。
 */
export type InboundRole = 'user' | 'system'

/**
 * 随提交记录的角色和来源元数据，与内容块独立。
 */
export interface MessageSubmissionOptions {
  role: InboundRole
  origin: string
}

/**
 * 提交给 Runtime 的服务端资产引用；此处不得包含本地对象 URL。
 */
export interface AssetSource {
  type: 'asset'
  asset_id: string
  media_type: string
  filename?: string | null
}

/**
 * 上传回执，包含完整性元数据和后续提交使用的来源引用。
 */
export interface AssetUploadResponse {
  asset_id: string
  media_type: string
  filename?: string | null
  size_bytes: number
  checksum_sha256: string
  source: AssetSource
}

/**
 * 支持的资产输入类型；图片块显式指定自动细节级别。
 */
export type InputMediaBlock =
  | { type: 'image'; source: AssetSource; detail: 'auto' }
  | { type: 'audio'; source: AssetSource }
  | { type: 'video'; source: AssetSource }
  | { type: 'document'; source: AssetSource }

/**
 * 单个有序输入块：文本或已上传资产。
 */
export type InputContentBlock = InputMediaBlock | { type: 'text'; text: string }

/**
 * 公开媒体元数据；内容块可描述来源而不暴露可获取的原始内容。
 */
type PublicMediaBlock<MediaType extends 'image' | 'audio' | 'video' | 'document'> = {
  type: MediaType
  source_type: 'url' | 'base64' | 'asset'
  media_type: string
  filename?: string | null
  asset_id?: string | null
}

/**
 * 公开历史中的媒体类型；当前资产读取器需要 asset_id。
 */
export type PublicMediaMessageBlock =
  | PublicMediaBlock<'image'>
  | PublicMediaBlock<'audio'>
  | PublicMediaBlock<'video'>
  | PublicMediaBlock<'document'>

/**
 * 输入消息气泡及待处理输入摘要支持的内容。
 */
export type PublicUserMessageBlock =
  | { type: 'text'; text: string }
  | PublicMediaMessageBlock

/**
 * 已接受但尚未执行的输入；驼峰字段由 HTTP 适配层映射。
 */
export interface PendingInput {
  messageId: string
  role: InboundRole
  origin: string
  content: string
  blocks: PublicUserMessageBlock[]
  mode: SubmissionMode
  createdAt: string
}

/**
 * 本地侧栏摘要，仅在提交获接受后创建，不包含消息历史。
 */
export interface SessionRecord {
  sessionId: string
  agentId: string
  title: string
  createdAt: string
  updatedAt: string
  titleVersion: number
}

/** 服务端排序的单页结果；不透明游标原样传回。 */
export interface SessionPage {
  sessions: SessionRecord[]
  nextCursor: string | null
}

/**
 * 公开的持久化消息封装；结构化内容块优先于旧版 content 字段。
 */
export interface PublicMessage {
  message_id: string
  role: 'user' | 'system' | 'assistant' | 'tool'
  origin: string
  content: string
  blocks?: PublicMessageBlock[]
  created_at: string
  is_error: boolean
}

/**
 * 用于消息渲染的公开内容块联合类型，包含执行活动和压缩标记。
 */
export type PublicMessageBlock =
  | PublicUserMessageBlock
  | { type: 'compaction' }
  | { type: 'thinking'; thinking: string }
  | {
      type: 'tool_call'
      tool_call_id?: string
      tool_name: string
      arguments?: Record<string, unknown>
      raw_arguments?: string
      truncated?: boolean
    }
  | {
      type: 'tool_result'
      tool_call_id?: string
      tool_name: string
      content?: string
      is_error: boolean
      truncated?: boolean
    }

/**
 * 后端 Token 用量；供应商特有计数器可能缺失或为 null。
 */
export interface TokenUsage {
  input_tokens: number
  output_tokens: number
  total_tokens: number
  accounting?: string
  reasoning_tokens?: number | null
  cache_read_tokens?: number | null
  cache_write_tokens?: number | null
}

/**
 * 后端运行时间戳和毫秒时长；挂起期间总耗时与活跃时长可能不同。
 */
export interface RunTiming {
  started_at?: string
  ended_at?: string
  elapsed_ms: number
  active_ms: number
}

/**
 * 附在会话快照上的可选终态错误、输出或挂起元数据。
 */
export interface PublicResult {
  content?: string | null
  error_code?: string | null
  error_message?: string | null
  resume_token?: string | null
  suspend_expires_at?: string | null
}

/**
 * 权威执行状态，与权威持久化消息历史分别维护。
 */
export interface SessionSnapshot {
  session_id: string
  status: SessionStatus
  result?: PublicResult | null
  usage?: TokenUsage | null
  timing?: RunTiming | null
}

/**
 * 带版本的 SSE 载荷；event_id 用于重放去重，与传输游标不同。
 */
export interface PublicEventEnvelope {
  schema_version: 1
  event_id: string
  type: string
  created_at: string
  session_id: string
  payload: Record<string, unknown>
}

/**
 * 简要的投影状态标记；对话渲染可将其排除在消息内容之外。
 */
export interface StatusTimelineItem {
  kind: 'status'
  key: string
  label: string
  detail?: string
  tone: 'neutral' | 'active' | 'success' | 'danger' | 'pending'
  createdAt: string
}

/**
 * 逐步累积的文本或思考块；已知 messageId 和 index 时具有稳定的历史身份。
 */
export interface BlockTimelineItem {
  kind: 'block'
  key: string
  blockId?: string
  messageId?: string
  index?: number
  blockType: string
  content: string
  completed: boolean
  durationMs?: number
  createdAt: string
}

/**
 * 以工具调用 ID 为键的生命周期投影，保留结果和可选耗时元数据。
 */
export interface ToolTimelineItem {
  kind: 'tool'
  key: string
  name: string
  status: 'running' | 'completed' | 'failed'
  arguments?: Record<string, unknown>
  content?: string
  durationMs?: number
  isError?: boolean
  truncated?: boolean
  reasonCode?: string
  createdAt: string
}

/**
 * 上下文压缩生命周期和 Token 数量，通过消息 ID 与历史记录对齐。
 */
export interface CompactionTimelineItem {
  kind: 'compaction'
  key: string
  messageId?: string
  status: 'running' | 'completed' | 'failed'
  action?: 'pruned' | 'summarized'
  beforeTokens?: number
  afterTokens?: number
  durationMs?: number
  createdAt: string
}

/**
 * 不显示的边界，用于将后续实时活动关联到输入消息。
 */
export interface MessageBoundaryTimelineItem {
  kind: 'message-boundary'
  key: string
  messageId: string
  createdAt: string
}

/**
 * 时间线投影和对话组装使用的可辨识联合类型。
 */
export type TimelineItem =
  | StatusTimelineItem
  | BlockTimelineItem
  | ToolTimelineItem
  | CompactionTimelineItem
  | MessageBoundaryTimelineItem

/**
 * 运行终态的展示摘要；挂起运行改用恢复字段。
 */
export interface TerminalSummary {
  status: 'succeeded' | 'failed' | 'canceled'
  usage?: TokenUsage
  timing?: RunTiming
  error?: { code?: string; message?: string; retryable?: boolean } | null
}

/**
 * 当前观察运行的临时事件投影、重放 ID 和游标。
 */
export interface StreamTimeline {
  items: TimelineItem[]
  appliedEventIds: string[]
  cursor?: string
  terminal?: TerminalSummary
  resumeToken?: string
  resumeExpiresAt?: string
}

/**
 * 存储的流恢复位置，可附带用于当前标签页恢复的临时投影。
 */
export interface SessionCheckpoint {
  cursor: string
  appliedEventIds: string[]
  timeline?: StreamTimeline
}

/** 会话权威待办列表中的有序条目。 */
export interface TodoItem {
  content: string
  status: 'pending' | 'in_progress' | 'completed'
}
