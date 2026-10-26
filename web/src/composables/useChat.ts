/**
 * 协调选中会话的历史、提交和实时执行状态。
 *
 * 此组合函数负责请求取消、乐观消息、SSE 检查点及 COMPLETE 模式轮询。
 * 视图版本号用于拒绝先前选中会话的响应；运行和终态版本区分同一视图中的相邻运行。
 * 后端消息和快照始终优先于临时界面投影。
 */

import {
  computed,
  onScopeDispose,
  ref,
  shallowRef,
  type ComputedRef,
  type Ref,
} from 'vue'

import { RuntimeHttpError, type RuntimeClient, type RuntimeScope } from '../api/client'
import {
  applyMessageStarted,
  applyPublicEvent,
  emptyTimeline,
} from '../chat/projection'
import type { AgentDefinition, RuntimeConfig } from '../config'
import type {
  InputContentBlock,
  MessageSubmissionOptions,
  PublicEventEnvelope,
  PendingInput,
  PublicMessage,
  PublicUserMessageBlock,
  SessionRecord,
  SessionSnapshot,
  StreamTimeline,
  SubmissionMode,
} from '../domain/types'
import { SseParser } from '../sse/parser'
import type { CheckpointStore } from '../storage/sessionStore'

import { sessionKey, useSessionCatalog } from './useSessionCatalog'
import { useTodos } from './useTodos'

const ACTIVE_STATUSES = new Set(['running', 'pending'])
const TERMINAL_STATUSES = new Set(['succeeded', 'failed', 'canceled'])

/**
 * 连接配置及已加载且非空的后端 Agent 目录。
 */
export type ChatRuntimeConfig = RuntimeConfig & { agents: AgentDefinition[] }

/**
 * 控制器依赖与确定性测试钩子。
 *
 * 生产环境提供真实 HTTP 和浏览器存储适配器；时间、ID 和等待函数可注入，
 * 便于测试在没有外部服务时验证异步顺序。
 */
export interface UseChatOptions {
  config: ChatRuntimeConfig
  client: RuntimeClient
  checkpointStore: CheckpointStore
  now?: () => Date
  randomId?: () => string
  sleep?: (milliseconds: number) => Promise<void>
  autoBootstrap?: boolean
}

/**
 * 面向工作台的选中对话响应式状态和操作。
 *
 * isBusy 跟踪观察和恢复工作，isSending 跟踪提交确认，
 * isRunning 仅由后端快照状态推导。操作可能先于运行结束返回；
 * 组件应展示这些状态，不应自行推断运行完成。
 */
export interface ChatController {
  sessions: Ref<SessionRecord[]>
  catalog: ReturnType<typeof useSessionCatalog>
  todos: ReturnType<typeof useTodos>
  selectedSessionId: Ref<string>
  selectedAgent: ComputedRef<AgentDefinition>
  messages: Ref<PublicMessage[]>
  pendingInputs: Ref<PendingInput[]>
  timeline: Ref<StreamTimeline>
  snapshot: Ref<SessionSnapshot | null>
  submissionMode: Ref<SubmissionMode>
  isBusy: Ref<boolean>
  isSending: Ref<boolean>
  isRunning: ComputedRef<boolean>
  error: Ref<string | null>
  /** 非致命的浏览器持久化警告，与后端请求错误独立。 */
  storageWarning: Ref<string | null>
  /** 选中未持久化的草稿；实际提交才创建服务端目录记录。 */
  createSession(agentId?: string): Promise<void>
  /** 恢复已存储的会话；服务端仍有活动任务时继续观察。 */
  selectSession(sessionId: string, agentId?: string): Promise<void>
  /** 通过新的本地草稿选择 Agent，不发送后端请求。 */
  selectAgent(agentId: string): Promise<void>
  /** 提交内容块；布尔返回值表示 HTTP 接受，不表示运行完成。 */
  send(
    content: InputContentBlock[],
    submissionOptionsInput?: MessageSubmissionOptions,
  ): Promise<boolean>
  /** 使用当前挂起 Token 恢复，并观察后续执行。 */
  resume(input?: Record<string, unknown>): Promise<void>
  /** 请求服务端取消，并对齐最终结果。 */
  cancel(): Promise<void>
  /** 重新加载权威状态并重连选中会话的观察器。 */
  reconnect(): Promise<void>
  /** 通过后端软删除，再清除检查点并更新选中状态。 */
  removeSession(sessionId: string, agentId?: string): Promise<void>
  /** 使用传输游标将收到的事件应用到选中视图。 */
  applyEvent(event: PublicEventEnvelope, cursor: string): void
}

/**
 * 为选中对话创建响应式控制器。
 *
 * @param options - 非空 Agent 目录、HTTP 和存储适配器及可选测试时钟。
 * @returns 工作台使用的响应式状态和操作；操作方法负责界面错误。
 * @remarks 可能立即恢复已存储历史。作用域销毁时取消活动观察器。
 * 存储失败后退化为内存状态并显示警告；不提供离线队列。
 */
export function useChat(options: UseChatOptions): ChatController {
  const now = options.now ?? (() => new Date())
  const randomId = options.randomId ?? (() => crypto.randomUUID())
  const sleep =
    options.sleep ?? ((ms) => new Promise((resolve) => setTimeout(resolve, ms)))
  const availableAgentIds = new Set(options.config.agents.map((agent) => agent.id))
  const storageWarning = ref<string | null>(null)
  let checkpointStorageAvailable = true
  const checkpointStore: CheckpointStore = {
    load: (sessionId) =>
      checkpointStorageAvailable
        ? accessStorage(() => options.checkpointStore.load(sessionId), null, true)
        : null,
    save: (sessionId, checkpoint) => {
      if (checkpointStorageAvailable)
        accessStorage(
          () => options.checkpointStore.save(sessionId, checkpoint),
          undefined,
          true,
        )
    },
    clear: (sessionId) => {
      if (checkpointStorageAvailable)
        accessStorage(() => options.checkpointStore.clear(sessionId), undefined, true)
    },
  }
  const catalog = useSessionCatalog(options.client, availableAgentIds)
  const sessions = catalog.sessions
  const initial = createRecord(options.config.agents[0]!.id)
  const draftRecord = ref(initial)

  const selectedSessionId = ref(initial.sessionId)
  const messages = ref<PublicMessage[]>([])
  const pendingInputs = ref<PendingInput[]>([])
  const timeline = ref<StreamTimeline>(emptyTimeline())
  const snapshot = shallowRef<SessionSnapshot | null>(null)
  const submissionMode = ref<SubmissionMode>('queue')
  const isBusy = ref(false)
  const isSending = ref(false)
  const error = ref<string | null>(null)
  /** 等待与权威持久化消息对齐的本地条目。 */
  const optimisticMessageIds = new Set<string>()
  /** COMPLETE 模式接受终态前必须覆盖的已接受输入。 */
  const completeAwaitedMessageIds = new Set<string>()
  /** 当前观察器的所有者；活动运行中的独立提交可使用另一信号。 */
  let activeController: AbortController | null = null
  /** 拥有已打开 SSE 读取器的视图版本号，允许提交复用该流。 */
  let streamGeneration: number | null = null
  /** 视图重置时递增，防止迟到请求覆盖其他会话。 */
  let viewGeneration = 0
  /** 检测提交确认期间收到的终态事件。 */
  let terminalRevision = 0
  /** 重连时区分新观察到的运行与上一次终态快照。 */
  let runRevision = 0
  let viewChosen = false
  let disposed = false

  const selectedRecord = computed(
    () =>
      sessions.value.find(
        (item) =>
          item.sessionId === selectedSessionId.value &&
          item.agentId === draftRecord.value.agentId,
      ) ?? draftRecord.value,
  )
  const selectedAgent = computed(() =>
    agentById(selectedRecord.value?.agentId ?? options.config.agents[0]!.id),
  )
  const isRunning = computed(() => snapshot.value?.status === 'running')
  const todos = useTodos(options.client)
  todos.reset(currentScope())

  if (options.autoBootstrap !== false) {
    const generation = viewGeneration
    void catalog.load().then(async () => {
      if (disposed || viewChosen || viewGeneration !== generation || isSending.value)
        return
      const first = sessions.value[0]
      if (first) await selectSession(first.sessionId, first.agentId)
    })
  }
  onScopeDispose(() => {
    disposed = true
    activeController?.abort()
  })

  /**
   * 选择新的本地草稿，并使上一视图的异步工作失效。
   *
   * @param agentId - 草稿所属 Agent；未知 ID 回退到目录首项。
   * @remarks 不持久化侧栏记录，也不发送后端请求。
   */
  async function createSession(agentId = selectedAgent.value.id): Promise<void> {
    viewChosen = true
    activeController?.abort()
    const record = createRecord(agentById(agentId).id)
    draftRecord.value = record
    selectedSessionId.value = record.sessionId
    resetView()
    isBusy.value = false
  }

  /**
   * 选择已知的服务端会话并恢复其权威状态。
   *
   * @param sessionId - 已存储的会话 ID；未知 ID 会被忽略。
   */
  async function selectSession(sessionId: string, agentId?: string): Promise<void> {
    viewChosen = true
    const record = sessions.value.find(
      (item) =>
        item.sessionId === sessionId &&
        item.agentId === (agentId ?? selectedAgent.value.id),
    )
    if (!record) return
    draftRecord.value = record
    selectedSessionId.value = sessionId
    await bootstrap()
  }

  /**
   * 通过选择新草稿切换 Agent，不持久化空对话。
   */
  async function selectAgent(agentId: string): Promise<void> {
    if (agentId === selectedAgent.value.id && messages.value.length === 0) return
    await createSession(agentId)
  }

  /**
   * 提交一条输入消息，并启动或复用执行观察。
   *
   * @param content - 有序文本和资产块；空提交会被忽略。
   * @param submissionOptionsInput - 可选角色与来源，默认均为 user。
   * @returns HTTP 接受后返回 true，提交被拒绝或失败时返回 false。
   * @remarks 活动运行将已接受输入显示为待处理；空闲提交产生乐观历史。
   * 观察在后台继续。目录失败只产生目录错误，不能将后端已接受的提交判为失败。
   */
  async function send(
    content: InputContentBlock[],
    submissionOptionsInput?: MessageSubmissionOptions,
  ): Promise<boolean> {
    if (content.length === 0 || isSending.value) return false
    if (catalog.removing.value === sessionKey(currentScope())) return false
    viewChosen = true
    const submissionOptions: MessageSubmissionOptions = submissionOptionsInput
      ? { ...submissionOptionsInput }
      : { role: 'user', origin: 'user' }
    const isActiveSubmission = isRunning.value
    const reusesStream =
      selectedAgent.value.outputMode === 'stream' && streamGeneration === viewGeneration
    const controller =
      isActiveSubmission || reusesStream ? new AbortController() : replaceController()
    const mode = submissionMode.value
    error.value = null
    isSending.value = true
    if (!isActiveSubmission) {
      isBusy.value = true
    }
    const messageId = randomId()
    const generation = viewGeneration
    const terminalRevisionAtSubmit = terminalRevision
    const scope = currentScope()
    if (!isActiveSubmission) {
      addOptimisticMessage(messageId, content, submissionOptions)
    }
    const record = selectedRecord.value
    try {
      await options.client.submitMessage(
        scope,
        {
          messageId,
          content,
          mode,
          ...submissionOptions,
        },
        controller.signal,
      )
      catalog.accepted(scope, record.titleVersion === 0)
      if (isCurrentGeneration(generation)) maybeNameConversation(scope)
      if (!isCurrentGeneration(generation)) return true
      isSending.value = false
      if (selectedAgent.value.outputMode === 'complete') {
        completeAwaitedMessageIds.add(messageId)
      }
      if (isActiveSubmission) {
        addPendingInput(messageId, content, mode, submissionOptions)
        return true
      }
      if (terminalRevision !== terminalRevisionAtSubmit) {
        if (activeController === controller) isBusy.value = false
        return true
      }
      if (selectedAgent.value.outputMode === 'stream') {
        if (reusesStream) return true
      }
      observeAcceptedRun(controller, generation)
      return true
    } catch (caught) {
      if (isCurrentGeneration(generation)) {
        if (!isActiveSubmission) removeOptimisticMessage(messageId)
        handleError(caught)
        isSending.value = false
        if (!isActiveSubmission && activeController === controller) {
          isBusy.value = false
        }
      }
      return false
    }
  }

  /**
   * 观察成功提交的运行，不让发送 Promise 持续等待。
   *
   * 只有仍拥有视图的控制器可以清除忙碌状态；过期视图的错误会被忽略。
   * 根据 Agent 选择 SSE 消费或 COMPLETE 模式轮询。
   */
  function observeAcceptedRun(controller: AbortController, generation: number): void {
    void (async () => {
      try {
        if (selectedAgent.value.outputMode === 'stream') {
          await consumeStream(controller.signal, true, generation)
        } else {
          await pollUntilSettled(controller.signal, generation)
        }
      } catch (caught) {
        if (isCurrentGeneration(generation)) handleError(caught)
      } finally {
        if (activeController === controller) isBusy.value = false
      }
    })()
  }

  /**
   * 使用投影或快照中的恢复 Token 继续挂起运行。
   *
   * @param input - 恢复载荷，默认是批准对象。
   * @remarks 没有 Token 或仍忙碌时不操作。替换活动观察器，等待流或轮询观察，
   * 并通过响应式错误状态报告当前视图的失败。
   */
  async function resume(
    input: Record<string, unknown> = { approved: true },
  ): Promise<void> {
    const token =
      timeline.value.resumeToken ?? snapshot.value?.result?.resume_token ?? undefined
    if (!token || isBusy.value) return
    const controller = replaceController()
    const generation = viewGeneration
    isBusy.value = true
    error.value = null
    try {
      await options.client.resume(currentScope(), token, input, controller.signal)
      if (selectedAgent.value.outputMode === 'stream') {
        await consumeStream(controller.signal, false, generation)
      } else {
        await pollUntilSettled(controller.signal, generation)
      }
    } catch (caught) {
      if (isCurrentGeneration(generation)) handleError(caught)
    } finally {
      if (activeController === controller) isBusy.value = false
    }
  }

  /**
   * 请求后端取消，再观察权威结果。
   *
   * 中止上一观察器只停止本地 I/O，本身不会取消服务端运行。
   * 错误显示在当前视图中，不向界面抛出。
   */
  async function cancel(): Promise<void> {
    const controller = replaceController()
    const generation = viewGeneration
    error.value = null
    try {
      await options.client.cancel(currentScope(), controller.signal)
      if (selectedAgent.value.outputMode === 'stream') {
        await consumeStream(controller.signal, false, generation)
      } else {
        await pollUntilSettled(controller.signal, generation)
      }
    } catch (caught) {
      if (isCurrentGeneration(generation)) handleError(caught)
    } finally {
      if (activeController === controller) isBusy.value = false
    }
  }

  /**
   * 替换活动请求控制器，并重新加载选中会话的状态。
   */
  async function reconnect(): Promise<void> {
    const controller = replaceController()
    error.value = null
    isBusy.value = true
    try {
      await bootstrapWith(controller)
    } catch (caught) {
      handleError(caught)
    } finally {
      if (activeController === controller) isBusy.value = false
    }
  }

  /**
   * 清除本地状态或更改选中视图前，先在服务端删除。
   *
   * @param sessionId - 要软删除的会话；后端保留其消息历史。
   */
  async function removeSession(sessionId: string, agentId?: string): Promise<void> {
    const scope = { sessionId, agentId: agentId ?? selectedAgent.value.id }
    if (!(await catalog.remove(scope)) || disposed) return
    checkpointStore.clear(checkpointKey(scope))
    if (sessionKey(currentScope()) !== sessionKey(scope)) return
    if (sessions.value[0])
      await selectSession(sessions.value[0].sessionId, sessions.value[0].agentId)
    else await createSession()
  }

  /**
   * 使用新观察器加载选中会话，并管理忙碌及错误状态。
   */
  async function bootstrap(): Promise<void> {
    const controller = replaceController()
    isBusy.value = true
    error.value = null
    try {
      await bootstrapWith(controller)
    } catch (caught) {
      handleError(caught)
    } finally {
      if (activeController === controller) isBusy.value = false
    }
  }

  /**
   * 恢复检查点，并对齐快照、消息和待处理输入。
   *
   * @param controller - 此次恢复过程中所有请求的所有者。
   * @remarks 重置视图会推进版本号。迟到响应会被忽略；允许快照或待处理资源缺失。
   * 活动任务按照 Agent 的输出模式继续观察。
   */
  async function bootstrapWith(controller: AbortController): Promise<void> {
    resetView()
    void todos.refresh()
    const generation = viewGeneration
    const checkpoint = checkpointStore.load(checkpointKey(currentScope()))
    if (checkpoint?.timeline) timeline.value = checkpoint.timeline
    const scope = currentScope()
    const [snapshotResult, messagesResult, pendingResult] = await Promise.allSettled([
      options.client.snapshot(scope, controller.signal),
      options.client.messages(scope, controller.signal),
      options.client.pendingInputs(scope, controller.signal),
    ])
    if (!isCurrentGeneration(generation)) return
    if (messagesResult.status === 'fulfilled') {
      applyAuthoritativeMessages(messagesResult.value)
    } else {
      throw messagesResult.reason
    }
    if (snapshotResult.status === 'fulfilled') applySnapshot(snapshotResult.value)
    else if (!isNotFound(snapshotResult.reason)) throw snapshotResult.reason
    if (pendingResult.status === 'fulfilled') {
      applyAuthoritativePending(
        pendingResult.value,
        snapshot.value?.status === 'running',
      )
    } else if (!isNotFound(pendingResult.reason)) {
      throw pendingResult.reason
    }

    const hasPendingInputs =
      pendingResult.status === 'fulfilled' && pendingResult.value.length > 0
    if (snapshot.value?.status === 'running' || hasPendingInputs) {
      if (selectedAgent.value.outputMode === 'stream') {
        isBusy.value = true
        await consumeStream(controller.signal, false, generation)
      } else {
        await pollUntilSettled(controller.signal, generation)
      }
    }
  }

  /**
   * 消费并重连选中会话的 SSE 流，限制重试次数。
   *
   * @param signal - 活动观察器拥有的取消信号。
   * @param isAwaitingAcceptedRun - 避免将先前的终态快照视为新运行。
   * @param generation - 异步工作开始前捕获的视图版本号。
   * @remarks 正常轮换不消耗四次失败重试额度。额度耗尽时报告可重连错误。
   * 无效游标会触发历史重建；挂起在连接关闭时停止观察。
   */
  async function consumeStream(
    signal: AbortSignal,
    isAwaitingAcceptedRun = false,
    generation = viewGeneration,
  ): Promise<void> {
    const scope = currentScope()
    const runRevisionAtConnect = runRevision
    let attempts = 0
    while (!signal.aborted && isCurrentGeneration(generation) && attempts < 4) {
      const cursor =
        timeline.value.cursor ?? checkpointStore.load(checkpointKey(scope))?.cursor
      try {
        const response = await options.client.events(scope, cursor, signal)
        if (!isCurrentGeneration(generation)) return
        streamGeneration = generation
        todos.reset(scope)
        void todos.refresh()
        let rotated = false
        try {
          rotated = await readEventResponse(response, signal, scope, generation)
        } finally {
          if (streamGeneration === generation) streamGeneration = null
        }
        if (signal.aborted || !isCurrentGeneration(generation)) return
        const latest = await options.client.snapshot(scope, signal)
        if (!isCurrentGeneration(generation)) return
        if (
          isAwaitingAcceptedRun &&
          isTerminalStatus(latest.status) &&
          runRevision === runRevisionAtConnect
        ) {
          if (rotated) {
            attempts = 0
            continue
          }
          attempts += 1
          await sleep(Math.min(500 * 2 ** attempts, 4000))
          continue
        }
        applySnapshot(latest)
        if (!ACTIVE_STATUSES.has(latest.status) || latest.status === 'pending') {
          if (isTerminalStatus(latest.status)) {
            await refreshAuthority(signal, true, scope, generation)
          }
          return
        }
        if (rotated) {
          attempts = 0
          continue
        }
      } catch (caught) {
        if (signal.aborted) throw caught
        if (isCursorRecoveryError(caught)) {
          checkpointStore.clear(checkpointKey(scope))
          timeline.value = emptyTimeline()
          await refreshAuthority(signal, false, scope, generation)
        } else if (!isRetryable(caught) || attempts >= 3) {
          throw caught
        }
      }
      attempts += 1
      await sleep(Math.min(500 * 2 ** attempts, 4000))
    }
    if (!signal.aborted && isCurrentGeneration(generation)) {
      throw new Error('连接多次中断，请重新读取以继续接收消息')
    }
  }

  /**
   * 读取 UTF-8 数据块，应用完整事件并持久化可续连检查点。
   *
   * @param response - 已校验且响应体可读取的 SSE 响应。
   * @param signal - 观察器取消信号。
   * @param scope - 用于写入检查点和刷新的已捕获会话身份。
   * @param generation - 拥有此响应的视图版本号。
   * @returns 服务端是否显式请求正常轮换连接。
   * @remarks 除 stream.rotate 外忽略无 ID 的帧。终态事件刷新权威历史后只保留边界游标。
   * 始终释放读取器锁；解析和传输错误交由流重试控制器处理。
   * 检查点失败会设置 storageWarning，但不中断事件投影。
   */
  async function readEventResponse(
    response: Response,
    signal: AbortSignal,
    scope: RuntimeScope,
    generation: number,
  ): Promise<boolean> {
    const reader = response.body!.getReader()
    const decoder = new TextDecoder()
    const parser = new SseParser()
    let rotated = false
    try {
      while (!signal.aborted) {
        const { value, done } = await reader.read()
        if (signal.aborted || !isCurrentGeneration(generation)) break
        if (done) break
        for (const frame of parser.push(decoder.decode(value, { stream: true }))) {
          if (frame.event === 'stream.rotate') {
            rotated = true
            continue
          }
          if (!frame.id) continue
          const event = parseEnvelope(frame.data)
          applyEvent(event, frame.id, generation)
          checkpointStore.save(checkpointKey(scope), {
            cursor: frame.id,
            appliedEventIds: timeline.value.appliedEventIds,
            timeline: timeline.value,
          })
          if (event.type === 'run.suspended') {
            snapshot.value = {
              session_id: selectedSessionId.value,
              status: 'pending',
              result: { resume_token: timeline.value.resumeToken },
            }
            isBusy.value = false
          } else if (isTerminalEvent(event.type)) {
            applyTerminalEvent(event)
            await refreshAuthority(signal, true, scope, generation)
            if (!isCurrentGeneration(generation)) return rotated
            retainBoundaryCursor(scope)
            isBusy.value = false
          }
        }
      }
    } finally {
      reader.releaseLock()
    }
    return rotated
  }

  /**
   * 轮询 COMPLETE 模式执行，直至历史确认终态结果或挂起。
   *
   * @param signal - 观察器取消信号。
   * @param generation - 视图所有者，请求完成后再次核对。
   * @throws Error 有限的 600 次轮询耗尽时抛出；除会话记录创建期间的临时 404 外，
   * 其他请求失败直接传播。
   * @remarks 未收敛的快照之间等待一秒，不包含请求耗时。
   */
  async function pollUntilSettled(
    signal: AbortSignal,
    generation = viewGeneration,
  ): Promise<void> {
    const scope = currentScope()
    for (let count = 0; count < 600 && !signal.aborted; count += 1) {
      let latest: SessionSnapshot
      try {
        latest = await options.client.snapshot(scope, signal)
      } catch (caught) {
        // 提交返回 202 后会话记录异步创建，短暂 404 属正常，继续轮询。
        if (!signal.aborted && isNotFound(caught)) {
          await sleep(1000)
          continue
        }
        throw caught
      }
      if (!isCurrentGeneration(generation)) return
      void todos.refresh()
      if (
        latest.status === 'running' &&
        snapshot.value !== null &&
        isTerminalStatus(snapshot.value.status)
      ) {
        beginRunProjection()
      }
      if (isTerminalStatus(latest.status)) {
        const isSettled = await confirmCompleteTerminal(
          latest,
          signal,
          scope,
          generation,
        )
        if (isSettled) return
      } else {
        applySnapshot(latest)
        if (catalog.needsMessage(scope)) {
          try {
            const history = await options.client.messages(scope, signal)
            if (!isCurrentGeneration(generation)) return
            applyAuthoritativeMessages(history, true)
            maybeNameConversation(scope)
          } catch (caught) {
            if (signal.aborted) throw caught
            if (!isCurrentGeneration(generation)) return
            catalog.namingFailed(scope, caught)
          }
        }
        if (latest.status === 'pending') return
      }
      await sleep(1000)
    }
    throw new Error('等待 Agent 完成超时，请稍后重新连接')
  }

  /**
   * 接受终态快照前对齐消息和待处理输入。
   *
   * @returns 已收敛，或视图已过期且观察器应停止时返回 true。
   * @remarks 上一次运行的终态快照不足以确认较新的已接受输入。
   */
  async function confirmCompleteTerminal(
    latest: SessionSnapshot,
    signal: AbortSignal,
    scope: RuntimeScope,
    generation: number,
  ): Promise<boolean> {
    const [authoritativeMessages, authoritativePending] = await Promise.all([
      options.client.messages(scope, signal),
      options.client.pendingInputs(scope, signal),
    ])
    if (!isCurrentGeneration(generation)) return true
    const isSettled = isCompleteTerminalSettled(
      latest,
      authoritativeMessages,
      authoritativePending,
    )
    applyAuthoritativeMessages(authoritativeMessages, !isSettled)
    applyAuthoritativePending(authoritativePending, !isSettled)
    if (isSettled) {
      applySnapshot(latest)
      maybeNameConversation(scope)
    }
    return isSettled
  }

  /**
   * 检查没有待处理输入，且终态覆盖了所有已接受输入。
   *
   * 只有集合中的消息得到覆盖后才清空等待集合，然后检查最新输入消息。
   * 此函数读取权威数组，但会修改本地等待集合。
   */
  function isCompleteTerminalSettled(
    latest: SessionSnapshot,
    authoritativeMessages: PublicMessage[],
    authoritativePending: PendingInput[],
  ): boolean {
    if (authoritativePending.length > 0) return false
    for (const messageId of completeAwaitedMessageIds) {
      if (!isMessageCoveredByTerminal(messageId, latest, authoritativeMessages)) {
        return false
      }
    }
    completeAwaitedMessageIds.clear()
    const latestInbound = [...authoritativeMessages]
      .reverse()
      .find((message) => message.role === 'user' || message.role === 'system')
    if (!latestInbound) return true
    return isMessageCoveredByTerminal(
      latestInbound.message_id,
      latest,
      authoritativeMessages,
    )
  }

  /**
   * 用服务端消息替换临时历史，并可选刷新快照。
   *
   * @param signal - 请求取消信号。
   * @param includeSnapshot - false 表示在游标恢复期间保留乐观消息。
   * @param scope - 已捕获的请求身份，默认是选中会话。
   * @param generation - 所有者版本号；过期响应不会更新选中视图。
   */
  async function refreshAuthority(
    signal: AbortSignal,
    includeSnapshot = true,
    scope = currentScope(),
    generation = viewGeneration,
  ): Promise<void> {
    if (isCurrentGeneration(generation)) void todos.refresh()
    if (includeSnapshot) {
      const [nextSnapshot, nextMessages] = await Promise.all([
        options.client.snapshot(scope, signal),
        options.client.messages(scope, signal),
      ])
      if (!isCurrentGeneration(generation)) return
      applySnapshot(nextSnapshot)
      applyAuthoritativeMessages(nextMessages)
      maybeNameConversation(scope)
    } else {
      const nextMessages = await options.client.messages(scope, signal)
      if (!isCurrentGeneration(generation)) return
      applyAuthoritativeMessages(nextMessages, true)
    }
  }

  /**
   * 使用注入的时间和 ID 提供器创建内存会话摘要。
   */
  function createRecord(agentId: string): SessionRecord {
    const timestamp = now().toISOString()
    return {
      sessionId: randomId(),
      agentId,
      title: '新对话',
      createdAt: timestamp,
      updatedAt: timestamp,
      titleVersion: 0,
    }
  }

  /** 遵循后端的首条用户文本规则；assistant 和后续输入不能替代。 */
  function maybeNameConversation(scope: RuntimeScope): void {
    const firstUser = messages.value.find((message) => message.role === 'user')
    if (firstUser?.content.trim() && !optimisticMessageIds.has(firstUser.message_id))
      catalog.messageAvailable(scope)
  }

  /**
   * 空闲提交获确认前先追加本地输入消息。
   */
  function addOptimisticMessage(
    messageId: string,
    content: InputContentBlock[],
    options: MessageSubmissionOptions,
  ): void {
    optimisticMessageIds.add(messageId)
    messages.value = [
      ...messages.value,
      {
        message_id: messageId,
        role: options.role,
        origin: options.origin,
        content: textFromBlocks(content),
        blocks: publicBlocks(content),
        created_at: now().toISOString(),
        is_error: false,
      },
    ]
  }

  /**
   * 移除被拒绝的乐观消息及其对齐标记。
   */
  function removeOptimisticMessage(messageId: string): void {
    optimisticMessageIds.delete(messageId)
    messages.value = messages.value.filter(
      (message) => message.message_id !== messageId,
    )
  }

  /**
   * 展示活动运行中已接受的输入，除非历史或待处理列表已有该消息。
   */
  function addPendingInput(
    messageId: string,
    content: InputContentBlock[],
    mode: SubmissionMode,
    options: MessageSubmissionOptions,
  ): void {
    if (
      messages.value.some((message) => message.message_id === messageId) ||
      pendingInputs.value.some((input) => input.messageId === messageId)
    ) {
      return
    }
    pendingInputs.value = [
      ...pendingInputs.value,
      {
        messageId,
        role: options.role,
        origin: options.origin,
        content: textFromBlocks(content),
        blocks: publicBlocks(content),
        mode,
        createdAt: now().toISOString(),
      },
    ]
  }

  /**
   * 将权威历史与乐观消息、待处理消息身份对齐。
   *
   * @param authoritative - 要安装的服务端历史。
   * @param preserveOptimistic - 执行尚未收敛时保留未匹配的乐观条目。
   * @remarks 已领取的待处理输入在其消息 ID 进入历史后立即消失。
   */
  function applyAuthoritativeMessages(
    authoritative: PublicMessage[],
    preserveOptimistic = false,
  ): void {
    const authoritativeIds = new Set(authoritative.map((message) => message.message_id))
    for (const messageId of authoritativeIds) optimisticMessageIds.delete(messageId)
    pendingInputs.value = pendingInputs.value.filter(
      (input) => !authoritativeIds.has(input.messageId),
    )
    if (!preserveOptimistic) {
      optimisticMessageIds.clear()
      messages.value = authoritative
      return
    }
    const pending = messages.value.filter(
      (message) =>
        optimisticMessageIds.has(message.message_id) &&
        !authoritativeIds.has(message.message_id),
    )
    messages.value = [...authoritative, ...pending]
  }

  /**
   * 根据可见历史对服务端待处理输入去重。
   *
   * @param authoritative - 服务端尚未领取的输入。
   * @param showAsPending - 运行中保留在待处理列表；否则将未见输入展示为输入消息，
   * 避免空闲视图隐藏已接受内容。
   */
  function applyAuthoritativePending(
    authoritative: PendingInput[],
    showAsPending: boolean,
  ): void {
    const messageIds = new Set(messages.value.map((message) => message.message_id))
    const unseen = authoritative.filter((input) => !messageIds.has(input.messageId))
    if (showAsPending) {
      pendingInputs.value = unseen
      return
    }
    pendingInputs.value = []
    messages.value = [
      ...messages.value,
      ...unseen.map((input) => ({
        message_id: input.messageId,
        role: input.role,
        origin: input.origin,
        content: input.content,
        blocks: input.blocks,
        created_at: input.createdAt,
        is_error: false,
      })),
    ]
  }

  /**
   * 将事件投影到当前视图，忽略旧视图拥有的响应。
   *
   * @param event - 公开事件封装；消息或运行开始也会更新控制器状态。
   * @param cursor - 与事件配对的传输游标。
   * @param generation - 所有者视图版本号，默认是选中视图。
   */
  function applyEvent(
    event: PublicEventEnvelope,
    cursor: string,
    generation = viewGeneration,
  ): void {
    if (!isCurrentGeneration(generation)) return
    if (event.type === 'message.started') {
      const next = applyMessageStarted(
        { messages: messages.value, pendingInputs: pendingInputs.value },
        event,
      )
      const messageId = stringField(event.payload.message, 'message_id')
      if (messageId) optimisticMessageIds.delete(messageId)
      messages.value = next.messages
      pendingInputs.value = next.pendingInputs
      maybeNameConversation(currentScope())
    } else if (event.type === 'run.started') {
      runRevision += 1
      beginRunProjection()
      snapshot.value = {
        session_id: selectedSessionId.value,
        status: 'running',
      }
      isBusy.value = true
    }
    timeline.value = applyPublicEvent(timeline.value, event, cursor)
    if (
      ((event.type === 'tool.completed' || event.type === 'tool.failed') &&
        event.payload.tool_name === 'todo_write') ||
      isTerminalEvent(event.type)
    ) {
      void todos.refresh()
    }
  }

  /**
   * 推进终态版本，并将运行终态事件映射为快照状态。
   */
  function applyTerminalEvent(event: PublicEventEnvelope): void {
    terminalRevision += 1
    const status =
      event.type === 'run.failed'
        ? 'failed'
        : event.type === 'run.canceled'
          ? 'canceled'
          : 'succeeded'
    applySnapshot({
      session_id: selectedSessionId.value,
      status,
      usage: tokenUsageValue(event.payload.usage),
      timing: runTimingValue(event.payload.timing),
      result:
        status === 'failed'
          ? {
              error_code: stringField(event.payload.error, 'code'),
              error_message: stringField(event.payload.error, 'message'),
            }
          : undefined,
    })
  }

  /**
   * 安装权威执行状态并合并用于展示的终态元数据。
   *
   * 后续快照省略用量或耗时时保留现有值；成功完成时清除过期错误详情。
   */
  function applySnapshot(latest: SessionSnapshot): void {
    snapshot.value = latest
    if (!isTerminalStatus(latest.status)) return
    const existing = timeline.value.terminal
    const result = latest.result
    const hasError = latest.status === 'failed' || latest.status === 'canceled'
    const error =
      hasError && (result?.error_code || result?.error_message)
        ? {
            code: result.error_code ?? undefined,
            message: result.error_message ?? undefined,
          }
        : hasError
          ? existing?.error
          : undefined
    timeline.value = {
      ...timeline.value,
      terminal: {
        status: latest.status,
        usage: latest.usage ?? existing?.usage,
        timing: latest.timing ?? existing?.timing,
        error,
      },
    }
  }

  /**
   * 保留最后的可续连传输游标，同时创建新的运行时间线。
   *
   * 清除投影条目和去重 ID，避免相邻运行共享视觉状态。
   */
  function beginRunProjection(): void {
    const cursor =
      timeline.value.cursor ??
      checkpointStore.load(checkpointKey(currentScope()))?.cursor
    timeline.value = { ...emptyTimeline(), cursor }
    if (cursor) {
      checkpointStore.save(checkpointKey(currentScope()), {
        cursor,
        appliedEventIds: [],
      })
    } else {
      checkpointStore.clear(checkpointKey(currentScope()))
    }
  }

  /**
   * 终态历史对齐后仅持久化游标。
   *
   * @param scope - 检查点所属身份，默认是选中视图。
   */
  function retainBoundaryCursor(scope = currentScope()): void {
    const cursor = timeline.value.cursor
    if (!cursor) {
      checkpointStore.clear(checkpointKey(scope))
      return
    }
    checkpointStore.save(checkpointKey(scope), {
      cursor,
      appliedEventIds: [],
    })
  }

  /**
   * 为一次请求或观察周期捕获选中的会话和 Agent ID。
   */
  function currentScope(): RuntimeScope {
    return { sessionId: selectedSessionId.value, agentId: selectedAgent.value.id }
  }

  /** 将每次检查点操作（包括延迟写入）限定到完整所有者身份。 */
  function checkpointKey(scope: RuntimeScope): string {
    return JSON.stringify([
      options.config.appId,
      options.config.userId,
      scope.agentId,
      scope.sessionId,
    ])
  }

  /**
   * 查找 Agent；找不到时回退到必需的非空目录首项。
   */
  function agentById(agentId: string): AgentDefinition {
    return (
      options.config.agents.find((agent) => agent.id === agentId) ??
      options.config.agents[0]!
    )
  }

  /**
   * 中止上一活动观察器，并将所有权转移给新控制器。
   */
  function replaceController(): AbortController {
    activeController?.abort()
    activeController = new AbortController()
    return activeController
  }

  /**
   * 推进视图版本号，清除临时消息、运行、错误和提交状态。
   */
  function resetView(): void {
    viewGeneration += 1
    todos.reset(currentScope())
    optimisticMessageIds.clear()
    completeAwaitedMessageIds.clear()
    messages.value = []
    pendingInputs.value = []
    isSending.value = false
    snapshot.value = null
    timeline.value = emptyTimeline()
    error.value = null
  }

  /**
   * 检查异步工作是否仍属于选中视图。
   */
  function isCurrentGeneration(generation: number): boolean {
    return generation === viewGeneration
  }

  /**
   * 展示错误消息，同时忽略预期的 DOM AbortError 取消。
   */
  function handleError(caught: unknown): void {
    if (caught instanceof DOMException && caught.name === 'AbortError') return
    error.value = caught instanceof Error ? caught.message : '发生未知错误'
  }

  /**
   * 将可选浏览器持久化与已接受消息及活动观察隔离。
   *
   * @param operation - 同步存储操作，可能因配额或策略失败。
   * @param fallback - 读取失败时使用的值；写入传入 undefined。
   * @param checkpoint - 失败后禁用此控制器的后续检查点 I/O。
   * @returns 操作结果或默认值，并单独显示用户可见的持久化警告。
   */
  function accessStorage<T>(operation: () => T, fallback: T, checkpoint = false): T {
    try {
      return operation()
    } catch {
      if (checkpoint) checkpointStorageAvailable = false
      storageWarning.value =
        '浏览器本地存储不可用，当前对话仍可继续；刷新后可能无法恢复本地记录。'
      return fallback
    }
  }

  return {
    sessions,
    catalog,
    todos,
    selectedSessionId,
    selectedAgent,
    messages,
    pendingInputs,
    timeline,
    snapshot,
    submissionMode,
    isBusy,
    isSending,
    isRunning,
    error,
    storageWarning,
    createSession,
    selectSession,
    selectAgent,
    send,
    resume,
    cancel,
    reconnect,
    removeSession,
    applyEvent,
  }
}

/**
 * 解析 JSON 并校验最低限度的公开事件封装。
 *
 * @param data - 以换行符拼接的 SSE data 字段内容。
 * @returns 版本 1 的事件封装；不深度检查载荷内部及其他字段。
 * @throws SyntaxError JSON 无效时抛出；封装结构不支持时抛出 Error。
 */
function parseEnvelope(data: string): PublicEventEnvelope {
  const value: unknown = JSON.parse(data)
  if (
    typeof value !== 'object' ||
    value === null ||
    !('schema_version' in value) ||
    value.schema_version !== 1 ||
    !('event_id' in value) ||
    typeof value.event_id !== 'string' ||
    !('type' in value) ||
    typeof value.type !== 'string' ||
    !('payload' in value) ||
    typeof value.payload !== 'object' ||
    value.payload === null
  ) {
    throw new Error('收到无法识别的 SSE 事件')
  }
  return value as PublicEventEnvelope
}

/**
 * 以换行符拼接文本块，不修改原始内容数组。
 */
function textFromBlocks(content: InputContentBlock[]): string {
  return content
    .filter((block) => block.type === 'text')
    .map((block) => block.text)
    .join('\n')
}

/**
 * 将输入资产引用映射为公开内容块元数据，用于乐观渲染。
 */
function publicBlocks(content: InputContentBlock[]): PublicUserMessageBlock[] {
  return content.map((block) => {
    if (block.type === 'text') return block
    return {
      type: block.type,
      source_type: 'asset',
      media_type: block.source.media_type,
      filename: block.source.filename,
      asset_id: block.source.asset_id,
    }
  })
}

/**
 * 识别运行终态事件；run.suspended 仍允许恢复。
 */
function isTerminalEvent(type: string): boolean {
  return ['run.completed', 'run.failed', 'run.canceled'].includes(type)
}

/**
 * 将快照状态收窄为 succeeded、failed 或 canceled。
 */
function isTerminalStatus(
  status: SessionSnapshot['status'],
): status is 'succeeded' | 'failed' | 'canceled' {
  return TERMINAL_STATUSES.has(status)
}

/**
 * 检查历史是否能证明某条输入消息属于已完成运行。
 *
 * 后续 assistant 消息可覆盖该输入。失败或取消的运行若没有 assistant 输出，
 * 其开始时间必须不早于输入时间戳。时间戳无效或缺失时无法确认，返回 false。
 */
function isMessageCoveredByTerminal(
  messageId: string,
  terminal: SessionSnapshot,
  messages: PublicMessage[],
): boolean {
  const inboundIndex = messages.findIndex(
    (message) =>
      message.message_id === messageId &&
      (message.role === 'user' || message.role === 'system'),
  )
  if (inboundIndex < 0) return false
  if (
    messages.slice(inboundIndex + 1).some((message) => message.role === 'assistant')
  ) {
    return true
  }
  if (terminal.status !== 'failed' && terminal.status !== 'canceled') return false
  const runStartedAt = terminal.timing?.started_at
  if (!runStartedAt) return false
  const runStartedTime = Date.parse(runStartedAt)
  const messageTime = Date.parse(messages[inboundIndex]!.created_at)
  return (
    !Number.isNaN(runStartedTime) &&
    !Number.isNaN(messageTime) &&
    runStartedTime >= messageTime
  )
}

/**
 * 识别服务端尚无会话快照时返回的 HTTP 404。
 */
function isNotFound(value: unknown): boolean {
  return value instanceof RuntimeHttpError && value.status === 404
}

/**
 * 识别需要移除检查点并对齐历史的游标错误。
 */
function isCursorRecoveryError(value: unknown): boolean {
  return (
    value instanceof RuntimeHttpError &&
    ['invalid_cursor', 'cursor_ahead', 'cursor_expired', 'snapshot_required'].includes(
      value.code,
    )
  )
}

/**
 * 允许重试传输或非 HTTP 错误、404、限流和服务端错误。
 */
function isRetryable(value: unknown): boolean {
  return (
    !(value instanceof RuntimeHttpError) ||
    value.status === 404 ||
    value.status === 429 ||
    value.status >= 500
  )
}

/**
 * 接受对象形状的用量元数据，不校验各个数值字段。
 */
function tokenUsageValue(value: unknown): SessionSnapshot['usage'] {
  return isRecord(value)
    ? (value as unknown as NonNullable<SessionSnapshot['usage']>)
    : undefined
}

/**
 * 接受对象形状的耗时元数据，不校验各个字段。
 */
function runTimingValue(value: unknown): SessionSnapshot['timing'] {
  return isRecord(value)
    ? (value as unknown as NonNullable<SessionSnapshot['timing']>)
    : undefined
}

/**
 * 从未知的对象形状数据中读取可选字符串。
 */
function stringField(value: unknown, key: string): string | undefined {
  if (!isRecord(value)) return undefined
  return typeof value[key] === 'string' ? value[key] : undefined
}

/**
 * 将未知值收窄为非 null、非数组的对象。
 */
function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}
