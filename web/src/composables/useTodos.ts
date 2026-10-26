/** 读取权威待办列表，按选中会话串行执行，与对话错误隔离。 */
import { onScopeDispose, ref } from 'vue'
import type { RuntimeClient, RuntimeScope } from '../api/client'
import type { TodoItem } from '../domain/types'

/** 合并密集事件，并拒绝旧会话或旧连接的结果。 */
export function useTodos(client: Pick<RuntimeClient, 'todos'>) {
  const items = ref<TodoItem[]>([])
  const error = ref(false)
  const loading = ref(false)
  let scope: RuntimeScope | null = null
  let controller = new AbortController()
  let active: Promise<void> | null = null
  let queued = false
  let disposed = false

  /** 使待完成的读取失效；重连同一会话时保留最近的列表。 */
  function reset(next: RuntimeScope): void {
    controller.abort()
    controller = new AbortController()
    active = null
    queued = false
    loading.value = false
    if (scope?.sessionId !== next.sessionId || scope?.agentId !== next.agentId) {
      items.value = []
      error.value = false
    }
    scope = { ...next }
  }

  /** 读取最新列表；请求期间再次刷新时，结束后再补一次读取。 */
  function refresh(): Promise<void> {
    if (!scope || disposed) return Promise.resolve()
    if (active) {
      queued = true
      return active
    }
    const owner = controller
    const identity = scope
    loading.value = true
    active = (async () => {
      do {
        queued = false
        try {
          const result = await client.todos(identity, owner.signal)
          if (owner.signal.aborted) return
          items.value = result
          error.value = false
        } catch {
          if (owner.signal.aborted) return
          error.value = true
        }
      } while (queued && !owner.signal.aborted)
    })().finally(() => {
      if (owner !== controller || disposed) return
      loading.value = false
      active = null
    })
    return active
  }

  onScopeDispose(() => {
    disposed = true
    controller.abort()
  })
  return { items, error, loading, reset, refresh }
}
