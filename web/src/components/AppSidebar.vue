<script setup lang="ts">
/**
 * 展示服务端对话元数据和 Runtime 可用性。
 *
 * 选择、创建和移除操作交由工作台处理。此组件不读取消息，也不删除服务端历史。
 */

import { nextTick, ref } from 'vue'
import type { RuntimeScope } from '../api/client'
import type { SessionRecord } from '../domain/types'
import { sessionKey } from '../composables/useSessionCatalog'

const props = defineProps<{
  sessions: SessionRecord[]
  selectedSessionId: string
  selectedAgentId: string
  loading: boolean
  hasMore: boolean
  catalogError: string | null
  renaming: string | null
  removing: string | null
  rename: (scope: RuntimeScope, title: string) => Promise<boolean>
  open: boolean
  runtimeAvailable: boolean
}>()

const emit = defineEmits<{
  create: []
  select: [sessionId: string, agentId: string]
  remove: [sessionId: string, agentId: string]
  'load-more': []
  retry: []
  close: []
}>()

const editing = ref<string | null>(null)
const title = ref('')
const titleInput = ref<HTMLInputElement[]>()

/** 将原生弹出层定位在视口内，避免被滚动列表裁切。 */
function placeActions(event: MouseEvent): void {
  const button = event.currentTarget as HTMLButtonElement
  const menu = button.parentElement?.querySelector<HTMLElement>('[popover]')
  if (!menu) return
  const bounds = button.getBoundingClientRect()
  menu.style.left = `${Math.max(8, bounds.right - 124)}px`
  menu.style.top = `${Math.min(bounds.bottom + 4, window.innerHeight - 88)}px`
}

/** 执行操作前关闭原生弹出层；外部点击和 Escape 由浏览器处理。 */
function closeActions(event: MouseEvent): void {
  ;(event.currentTarget as HTMLElement).closest<HTMLElement>('[popover]')?.hidePopover()
}

/** 聚焦行内编辑器，不改变当前选中的对话。 */
async function edit(record: SessionRecord, event: MouseEvent): Promise<void> {
  closeActions(event)
  editing.value = sessionKey(record)
  title.value = record.title
  await nextTick()
  titleInput.value?.[0]?.focus()
  titleInput.value?.[0]?.select()
}

/** 关闭操作菜单后再交由上层删除。 */
function remove(record: SessionRecord, event: MouseEvent): void {
  closeActions(event)
  emit('remove', record.sessionId, record.agentId)
}

/** 失败时保持编辑器打开，避免乐观替换现有标题。 */
async function save(record: SessionRecord): Promise<void> {
  const key = sessionKey(record)
  if (await props.rename(record, title.value)) {
    if (editing.value === key) editing.value = null
  }
}

/**
 * 当天使用本地化标签，其他日期按本地月份和日期格式化。
 */
function dayLabel(value: string): string {
  const date = new Date(value)
  const today = new Date()
  if (date.toDateString() === today.toDateString()) return '今天'
  return new Intl.DateTimeFormat('zh-CN', { month: 'short', day: 'numeric' }).format(
    date,
  )
}
</script>

<template>
  <div v-if="open" class="sidebar-backdrop" @click="emit('close')" />
  <aside class="sidebar" :class="{ open }">
    <div class="brand">
      <span class="brand-mark">
        <svg viewBox="0 0 28 28" aria-hidden="true">
          <path d="m14 3-3 7-7 4 7 4 3 7 3-7 7-4-7-4-3-7Z" />
        </svg>
      </span>
      <div><strong>Agent Studio</strong></div>
      <button
        class="mobile-close"
        type="button"
        aria-label="关闭侧栏"
        @click="emit('close')"
        >×</button
      >
    </div>

    <button class="new-chat" type="button" @click="emit('create')">
      <svg viewBox="0 0 20 20" aria-hidden="true">
        <path d="M10 4v12M4 10h12" />
      </svg>
      新建对话
    </button>

    <div class="history-heading"
      ><span>最近对话</span><small>{{ sessions.length }}</small></div
    >
    <nav class="history-list" aria-label="历史对话">
      <div v-if="loading && sessions.length === 0" class="history-empty" role="status"
        >正在加载…</div
      >
      <div v-else-if="sessions.length === 0 && !catalogError" class="history-empty"
        >还没有历史对话</div
      >
      <div
        v-for="session in sessions"
        :key="sessionKey(session)"
        class="history-item"
        :class="{
          active:
            session.sessionId === selectedSessionId &&
            session.agentId === selectedAgentId,
        }"
      >
        <form
          v-if="editing === sessionKey(session)"
          class="history-title-editor"
          @submit.prevent="save(session)"
          @keydown.esc.prevent="editing = null"
        >
          <input
            ref="titleInput"
            v-model="title"
            aria-label="对话标题"
            :disabled="renaming === sessionKey(session)"
          />
          <button
            type="submit"
            aria-label="保存标题"
            :disabled="
              !title.trim() || [...title.trim()].length > 200 || renaming !== null
            "
            >✓</button
          >
          <button type="button" aria-label="取消改名" @click="editing = null">×</button>
        </form>
        <template v-else>
          <button
            type="button"
            class="history-select"
            @click="emit('select', session.sessionId, session.agentId)"
          >
            <span class="history-icon">
              <svg viewBox="0 0 20 20" aria-hidden="true">
                <path d="M4 5h12v8H8l-4 3V5Z" />
              </svg>
            </span>
            <span class="history-copy">
              <strong>{{ session.title }}</strong
              ><small>{{ dayLabel(session.createdAt) }}</small>
            </span>
          </button>
          <div class="history-actions">
            <button
              class="history-more"
              type="button"
              aria-label="对话操作"
              :popovertarget="`session-actions-${encodeURIComponent(sessionKey(session))}`"
              :aria-busy="removing === sessionKey(session)"
              @click="placeActions"
            >
              <svg viewBox="0 0 20 20" aria-hidden="true">
                <circle cx="10" cy="4" r="1.5" />
                <circle cx="10" cy="10" r="1.5" />
                <circle cx="10" cy="16" r="1.5" />
              </svg>
            </button>
            <div
              :id="`session-actions-${encodeURIComponent(sessionKey(session))}`"
              class="history-action-menu"
              popover="auto"
            >
              <button
                type="button"
                :disabled="removing === sessionKey(session)"
                @click="edit(session, $event)"
                >重命名</button
              >
              <button
                type="button"
                class="history-delete-action"
                :disabled="removing !== null"
                @click="remove(session, $event)"
                >删除</button
              >
            </div>
          </div>
        </template>
      </div>
      <button
        v-if="hasMore"
        class="history-load-more"
        :disabled="loading"
        @click="emit('load-more')"
        >加载更多</button
      >
      <div v-if="catalogError" class="history-error" role="alert"
        ><span>{{ catalogError }}</span
        ><button type="button" :disabled="loading" @click="emit('retry')"
          >重新读取</button
        ></div
      >
    </nav>

    <footer class="sidebar-footer">
      <span
        class="connection-dot"
        :class="{ available: runtimeAvailable }"
        role="img"
        :aria-label="runtimeAvailable ? 'Runtime API 可用' : 'Runtime API 不可用'"
        :title="runtimeAvailable ? 'Runtime API 可用' : 'Runtime API 不可用'"
      />
      <slot name="agent" />
    </footer>
  </aside>
</template>
