<script setup lang="ts">
/** 当前对话中只读且有序的任务进度。 */
import { computed, ref } from 'vue'
import type { TodoItem } from '../domain/types'

const props = defineProps<{
  todos: TodoItem[]
  error: boolean
  loading: boolean
}>()
defineEmits<{ retry: [] }>()
const expanded = ref(false)
const completed = computed(
  () => props.todos.filter((item) => item.status === 'completed').length,
)
const summary = computed(() => {
  const active = props.todos.filter((item) => item.status === 'in_progress')
  if (!active.length) return ''
  return active.length > 1
    ? `${active.length} 项 · ${active[0]!.content}`
    : active[0]!.content
})
const labels = { pending: '待开始', in_progress: '进行中', completed: '已完成' }
</script>

<template>
  <section v-if="todos.length || error" class="todo-panel" aria-label="任务清单">
    <button
      v-if="todos.length"
      class="todo-toggle"
      type="button"
      :aria-expanded="expanded"
      @click="expanded = !expanded"
    >
      <span class="todo-title-row">
        <svg class="todo-list-icon" viewBox="0 0 20 20" aria-hidden="true">
          <path d="m3 5 1 1 2-2M9 5h8M3 10h3m3 0h8M3 15h3m3 0h8" />
        </svg>
        <span class="todo-heading">任务清单</span>
        <span class="todo-count">已完成 {{ completed }} / {{ todos.length }}</span>
        <svg
          class="todo-chevron"
          :class="{ expanded }"
          viewBox="0 0 20 20"
          aria-hidden="true"
        >
          <path d="m6 8 4 4 4-4" />
        </svg>
      </span>
      <span v-if="!expanded && summary" class="todo-summary-row">
        <svg class="todo-spinner" viewBox="0 0 20 20" aria-hidden="true">
          <path d="M10 4a6 6 0 1 1-6 6" />
        </svg>
        <span class="todo-summary" :title="summary">{{ summary }}</span>
      </span>
    </button>
    <ol v-if="todos.length && expanded" class="todo-items">
      <li v-for="(todo, index) in todos" :key="index" :data-status="todo.status">
        <span class="todo-number" aria-hidden="true">{{ index + 1 }}.</span>
        <svg
          class="todo-status-icon"
          :class="{ 'todo-spinner': todo.status === 'in_progress' }"
          viewBox="0 0 20 20"
          aria-hidden="true"
        >
          <path v-if="todo.status === 'completed'" d="m4 10 4 4 8-8" />
          <path v-else-if="todo.status === 'in_progress'" d="M10 4a6 6 0 1 1-6 6" />
          <template v-else>
            <circle cx="10" cy="10" r="6" />
            <path d="M10 6v4l3 2" />
          </template>
        </svg>
        <span class="todo-content" :title="todo.content">{{ todo.content }}</span>
        <span class="todo-status">{{ labels[todo.status] }}</span>
      </li>
    </ol>
    <div v-if="error" class="todo-error" role="status">
      <span>任务清单暂未同步</span>
      <button type="button" :disabled="loading" @click="$emit('retry')">{{
        loading ? '重试中…' : '重试'
      }}</button>
    </div>
  </section>
</template>
