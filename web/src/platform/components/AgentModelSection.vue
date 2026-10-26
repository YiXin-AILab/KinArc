<script setup lang="ts">
import { computed } from 'vue'

import type { AgentFormState } from '../useAgentForm'

const props = defineProps<{
  form: AgentFormState
  /** 服务端/本地校验出的模型相关错误信息（context_window 等）。 */
  modelError?: string | null
}>()

const apiKeyInvalid = computed(() => {
  const value = props.form.apiKey.trim()
  return value !== '' && !/^\$\{[A-Za-z_][A-Za-z0-9_]*\}$/.test(value)
})

const fillStyle = computed(() => ({
  '--fill': `${(props.form.temperature / 2) * 100}%`,
}))
</script>

<template>
  <div class="platform-fsec" id="sec-model">
    <h3
      >模型
      <span class="platform-mut" style="font-size: 12px; font-weight: 400"
        >· LiteLLM Provider</span
      ></h3
    >
    <div v-if="modelError" class="platform-field-error">✕ {{ modelError }}</div>
    <div class="platform-frow2">
      <div class="platform-field">
        <label>model</label>
        <input
          v-model="form.model"
          type="text"
          class="platform-mono"
          placeholder="claude-sonnet-4"
          aria-label="model"
        />
      </div>
      <div class="platform-field">
        <label>base_url</label>
        <input
          v-model="form.baseUrl"
          type="text"
          class="platform-mono"
          placeholder="https://…/v1"
          aria-label="base_url"
        />
      </div>
    </div>
    <div class="platform-field">
      <label>
        api_key
        <span class="platform-badge platform-b-green" style="margin-left: 4px"
          >仅环境变量引用</span
        >
      </label>
      <input
        v-model="form.apiKey"
        type="text"
        class="platform-mono"
        :class="{ 'platform-input-invalid': apiKeyInvalid }"
        placeholder="${VAR_NAME}，留空则不设置"
        aria-label="api_key"
      />
      <div v-if="apiKeyInvalid" class="platform-hint">
        ⚠ 只接受 ${VAR} 环境变量引用格式或留空，平台不存储明文密钥
      </div>
    </div>
    <div class="platform-frow3">
      <div class="platform-field">
        <label
          >temperature ·
          <span class="platform-mono" style="color: var(--p-cyan)">{{
            form.temperature.toFixed(1)
          }}</span></label
        >
        <input
          v-model.number="form.temperature"
          type="range"
          min="0"
          max="2"
          step="0.1"
          :style="fillStyle"
          aria-label="temperature"
        />
      </div>
      <div class="platform-field">
        <label>max_output_tokens</label>
        <input
          v-model.number="form.maxOutputTokens"
          type="number"
          class="platform-mono"
          aria-label="max_output_tokens"
        />
      </div>
      <div class="platform-field">
        <label>context_window</label>
        <input
          v-model.number="form.contextWindow"
          type="number"
          class="platform-mono"
          aria-label="context_window"
        />
      </div>
    </div>
    <div class="platform-frow3">
      <div class="platform-field">
        <label>timeout_seconds</label>
        <input
          v-model.number="form.timeoutSeconds"
          type="number"
          class="platform-mono"
          aria-label="timeout_seconds"
        />
      </div>
      <div class="platform-field">
        <label>parallel_tool_calls</label>
        <div style="display: flex; align-items: center; gap: 9px; height: 35px">
          <label class="platform-switch">
            <input
              v-model="form.parallelToolCalls"
              type="checkbox"
              aria-label="parallel_tool_calls"
            /><i />
          </label>
          <span class="platform-mut" style="font-size: 12.5px">{{
            form.parallelToolCalls ? '开启' : '关闭'
          }}</span>
        </div>
      </div>
      <div />
    </div>
  </div>
</template>
