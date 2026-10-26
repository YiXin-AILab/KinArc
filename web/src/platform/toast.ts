import { reactive } from 'vue'

export interface ToastItem {
  id: number
  message: string
  kind: 'success' | 'error'
}

const state = reactive({ items: [] as ToastItem[] })
let nextId = 1

/** 轻量 toast：模块级状态，PlatformToast 组件统一渲染。 */
export function showToast(
  message: string,
  kind: 'success' | 'error' = 'success',
): void {
  const item: ToastItem = { id: nextId++, message, kind }
  state.items.push(item)
  setTimeout(() => {
    const index = state.items.findIndex((entry) => entry.id === item.id)
    if (index >= 0) state.items.splice(index, 1)
  }, 2600)
}

export function useToasts(): { items: ToastItem[] } {
  return state
}
