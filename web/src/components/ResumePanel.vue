<script setup lang="ts">
/**
 * 收集用于恢复挂起运行的 JSON 对象。
 *
 * 组件校验对象形状并发出输入；恢复 Token、HTTP 请求及后续执行观察
 * 由对话控制器负责。
 */

import { ref } from 'vue'

defineProps<{ expiresAt?: string }>()
const emit = defineEmits<{
  resume: [input: Record<string, unknown>]
}>()
const detailsOpen = ref(false)
const input = ref('{\n  "approved": true\n}')
const parseError = ref('')

/**
 * 校验编辑后的 JSON 是否为对象，并发出恢复意图。
 *
 * 解析和形状错误只留在面板内；失败时不发出事件。
 * 执行授权和一次性 Token 处理由后端及控制器负责。
 */
function resume(): void {
  try {
    const value: unknown = JSON.parse(input.value)
    if (typeof value !== 'object' || value === null || Array.isArray(value)) {
      throw new Error('输入必须是 JSON 对象')
    }
    parseError.value = ''
    emit('resume', value as Record<string, unknown>)
  } catch (error) {
    parseError.value = error instanceof Error ? error.message : 'JSON 格式错误'
  }
}
</script>

<template>
  <section class="resume-panel">
    <div class="resume-copy">
      <span class="resume-icon">!</span>
      <div>
        <strong>Agent 正在等待你的确认</strong>
        <p>当前连接已安全暂停，确认后会从原位置继续。</p>
      </div>
    </div>
    <div v-if="detailsOpen" class="resume-details">
      <label for="resume-input">Continuation input（JSON）</label>
      <textarea id="resume-input" v-model="input" rows="4" />
      <small v-if="parseError" class="field-error">{{ parseError }}</small>
    </div>
    <div class="resume-actions">
      <button type="button" class="ghost-button" @click="detailsOpen = !detailsOpen">
        {{ detailsOpen ? '收起参数' : '编辑参数' }}
      </button>
      <button type="button" class="primary-button" @click="resume">确认并继续</button>
    </div>
  </section>
</template>
