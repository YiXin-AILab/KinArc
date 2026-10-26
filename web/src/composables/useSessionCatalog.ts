/** 服务端管理的对话元数据，以及前端触发的一次性标题生成。 */
import { onScopeDispose, ref } from 'vue'

import { RuntimeHttpError, type RuntimeClient, type RuntimeScope } from '../api/client'
import type { SessionRecord } from '../domain/types'

/** 在当前用户范围内区分不同 Agent 的会话。 */
export function sessionKey(scope: RuntimeScope): string {
  return JSON.stringify([scope.agentId, scope.sessionId])
}

/**
 * 加载服务端分页，并按版本对齐标题响应。
 *
 * 只有当前挂载工作台中获接受的对话才可自动命名。持久化的公开文本触发一次尝试；
 * 历史会话、页面刷新和重试不会安排模型调用。删除由服务端负责；
 * 内存中的墓碑标记只用于阻止过期响应恢复已删除条目。
 */
export function useSessionCatalog(client: RuntimeClient, agentIds: Set<string>) {
  const sessions = ref<SessionRecord[]>([])
  const nextCursor = ref<string | null>(null)
  const loading = ref(false)
  const error = ref<string | null>(null)
  const renaming = ref<string | null>(null)
  const removing = ref<string | null>(null)
  const controller = new AbortController()
  let revision = 0
  let loadedPages = 1
  const deleted = new Set<string>()
  const registered = new Set<string>()
  const eligible = new Map<string, RuntimeScope>()
  const available = new Set<string>()
  onScopeDispose(() => controller.abort())

  /** 旧列表请求较晚完成时，保留更新的命令响应。 */
  function merge(records: SessionRecord[], firstPage = false): void {
    const previousRecords = new Map(
      sessions.value.map((record) => [sessionKey(record), record]),
    )
    const byKey = firstPage
      ? new Map<string, SessionRecord>()
      : new Map(previousRecords)
    for (const record of records) {
      const key = sessionKey(record)
      if (!agentIds.has(record.agentId) || deleted.has(key)) continue
      const previous = previousRecords.get(key)
      byKey.set(
        key,
        previous && previous.titleVersion > record.titleVersion ? previous : record,
      )
    }
    sessions.value = [...byKey.values()]
  }

  /** 刷新已加载分页或追加一页；过期请求不能修改目录。 */
  async function load(more = false): Promise<void> {
    if (more && (loading.value || nextCursor.value === null)) return
    const requestRevision = ++revision
    loading.value = true
    error.value = null
    try {
      const records: SessionRecord[] = []
      let cursor = more ? nextCursor.value : null
      let fetchedPages = 0
      do {
        const page = await client.sessions(cursor ?? undefined, controller.signal)
        if (controller.signal.aborted || requestRevision !== revision) return
        records.push(...page.sessions)
        cursor = page.nextCursor
        fetchedPages += 1
      } while (!more && cursor !== null && fetchedPages < loadedPages)
      merge(records, !more)
      loadedPages = more ? loadedPages + fetchedPages : fetchedPages
      nextCursor.value = cursor
      for (const scope of eligible.values()) maybeGenerate(scope)
    } catch (caught) {
      if (!controller.signal.aborted && requestRevision === revision) report(caught)
    } finally {
      if (requestRevision === revision) loading.value = false
    }
  }

  /** 登记已接受的提交，目录失败不能使消息发送变为失败。 */
  function accepted(scope: RuntimeScope, isNew: boolean): void {
    const key = sessionKey(scope)
    if (isNew && !registered.has(key)) {
      registered.add(key)
      eligible.set(key, { ...scope })
    }
    void load()
  }

  /** 公开文本在权威历史中得到确认后调用。 */
  function messageAvailable(scope: RuntimeScope): void {
    if (!eligible.has(sessionKey(scope))) return
    available.add(sessionKey(scope))
    maybeGenerate(scope)
  }

  /** COMPLETE 模式只读取足以生成标题的历史上下文。 */
  function needsMessage(scope: RuntimeScope): boolean {
    const key = sessionKey(scope)
    return (
      eligible.has(key) &&
      !available.has(key) &&
      sessions.value.some((record) => sessionKey(record) === key)
    )
  }

  /** 获取上下文失败时停止辅助命名，不中断回答观察。 */
  function namingFailed(scope: RuntimeScope, caught: unknown): void {
    eligible.delete(sessionKey(scope))
    available.delete(sessionKey(scope))
    report(caught)
  }

  /** 在 I/O 前消耗命名资格，避免重复观察提交重复调用。 */
  function maybeGenerate(scope: RuntimeScope): void {
    const key = sessionKey(scope)
    if (!eligible.has(key) || !available.has(key)) return
    const record = sessions.value.find((item) => sessionKey(item) === key)
    if (!record) return
    eligible.delete(key)
    available.delete(key)
    if (record.titleVersion !== 1 || deleted.has(key)) return
    void client
      .generateSessionTitle(scope, controller.signal)
      .then((updated) => {
        if (!controller.signal.aborted) merge([updated])
      })
      .catch(async (caught: unknown) => {
        if (controller.signal.aborted || deleted.has(key)) return
        if (caught instanceof RuntimeHttpError && caught.status === 409) await load()
        report(caught)
      })
  }

  /** 手动改名意图立即禁用自动命名，即使请求仍在处理中。 */
  async function rename(scope: RuntimeScope, title: string): Promise<boolean> {
    const value = title.trim()
    if (
      !value ||
      [...value].length > 200 ||
      renaming.value !== null ||
      removing.value === sessionKey(scope)
    )
      return false
    const key = sessionKey(scope)
    eligible.delete(key)
    renaming.value = key
    error.value = null
    try {
      const updated = await client.renameSession(scope, value, controller.signal)
      if (controller.signal.aborted) return false
      merge([updated])
      return true
    } catch (caught) {
      if (!controller.signal.aborted) report(caught)
      return false
    } finally {
      renaming.value = null
    }
  }

  /** 后端接受后才移除条目；失败时保留条目以便重试。 */
  async function remove(scope: RuntimeScope): Promise<boolean> {
    if (removing.value !== null) return false
    const key = sessionKey(scope)
    removing.value = key
    error.value = null
    try {
      await client.deleteSession(scope, controller.signal)
      if (controller.signal.aborted) return false
      deleted.add(key)
      eligible.delete(key)
      available.delete(key)
      sessions.value = sessions.value.filter((record) => sessionKey(record) !== key)
      return true
    } catch (caught) {
      if (!controller.signal.aborted) report(caught)
      return false
    } finally {
      removing.value = null
    }
  }

  /** 目录错误与消息错误分开处理，不提供重新提交消息的操作。 */
  function report(caught: unknown): void {
    error.value = caught instanceof Error ? caught.message : '对话列表更新失败'
  }

  return {
    sessions,
    nextCursor,
    loading,
    error,
    renaming,
    removing,
    load,
    accepted,
    messageAvailable,
    needsMessage,
    namingFailed,
    rename,
    remove,
  }
}
