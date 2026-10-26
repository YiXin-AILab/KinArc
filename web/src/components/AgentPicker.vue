<script setup lang="ts">
/**
 * 展示当前 Agent，并在目录选择器中发出选择变更。
 *
 * 父组件负责会话选择。在这里选择 Agent 只会发出其 ID 并关闭菜单，
 * 不会创建或持久化后端对话。
 */

import { onBeforeUnmount, onMounted, ref } from 'vue'

import type { AgentDefinition } from '../config'

defineProps<{
  agents: AgentDefinition[]
  agent: AgentDefinition
}>()

const emit = defineEmits<{
  'agent-change': [agentId: string]
}>()

const picker = ref<HTMLDetailsElement | null>(null)

/**
 * 选择器已挂载时关闭原生 details 元素。
 */
function closePicker(): void {
  if (picker.value) picker.value.open = false
}

/**
 * 发出选中的 ID、关闭选择器，并将焦点还给 summary 控件。
 */
function selectAgent(agentId: string): void {
  emit('agent-change', agentId)
  closePicker()
  picker.value?.querySelector('summary')?.focus()
}

/**
 * 指针事件发生在 DOM 子树外时关闭选择器。
 */
function onOutsidePointerDown(event: PointerEvent): void {
  if (event.target instanceof Node && !picker.value?.contains(event.target)) {
    closePicker()
  }
}

/**
 * 按 Escape 时关闭选择器并将焦点恢复到 summary。
 */
function onEscape(): void {
  closePicker()
  picker.value?.querySelector('summary')?.focus()
}

onMounted(() => document.addEventListener('pointerdown', onOutsidePointerDown))
onBeforeUnmount(() => document.removeEventListener('pointerdown', onOutsidePointerDown))
</script>

<template>
  <details ref="picker" class="agent-select" @keydown.esc.prevent="onEscape">
    <summary aria-label="选择 Agent" :title="agent.name">
      <span>{{ agent.name }}</span>
      <svg viewBox="0 0 20 20" aria-hidden="true">
        <path d="m6 8 4 4 4-4" />
      </svg>
    </summary>
    <div class="agent-menu">
      <button
        v-for="option in agents"
        :key="option.id"
        type="button"
        class="agent-option"
        :data-agent-id="option.id"
        :aria-current="option.id === agent.id ? 'true' : undefined"
        @click="selectAgent(option.id)"
      >
        <span>
          <strong>{{ option.name }}</strong>
          <small v-if="option.description">{{ option.description }}</small>
        </span>
        <svg v-if="option.id === agent.id" viewBox="0 0 20 20" aria-hidden="true">
          <path d="m4 10 4 4 8-8" />
        </svg>
      </button>
    </div>
  </details>
</template>
