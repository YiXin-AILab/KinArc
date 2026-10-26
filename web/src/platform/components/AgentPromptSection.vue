<script setup lang="ts">
import { computed, ref } from 'vue'

import type { AgentFormState } from '../useAgentForm'

const props = defineProps<{ form: AgentFormState }>()

const previewing = ref(false)
const charCount = computed(() => props.form.promptText.length)
</script>

<template>
  <div class="platform-fsec" id="sec-prompt">
    <h3>Prompt</h3>
    <div class="platform-field">
      <label>
        系统 Prompt
        <span class="platform-mut" style="font-weight: 400"> · 存为 </span>
        <span class="platform-mono" style="font-size: 11px; color: var(--p-cyan)"
          >prompt/system.md</span
        >
      </label>
      <div style="display: flex; gap: 6px; margin-bottom: 8px">
        <span
          class="platform-badge"
          :class="previewing ? 'platform-b-mut' : 'platform-b-cyan'"
          style="cursor: pointer"
          @click="previewing = false"
          >编辑</span
        >
        <span
          class="platform-badge"
          :class="previewing ? 'platform-b-cyan' : 'platform-b-mut'"
          style="cursor: pointer"
          @click="previewing = true"
          >预览</span
        >
        <span
          class="platform-mut"
          style="font-size: 11.5px; margin-left: auto; align-self: center"
        >
          支持 ${ENV_VAR} 插值 · {{ charCount }} 字符
        </span>
      </div>
      <textarea
        v-if="!previewing"
        v-model="form.promptText"
        rows="9"
        class="platform-mono"
        style="line-height: 1.7"
        placeholder="# 角色&#10;你是…"
        aria-label="系统 Prompt"
      />
      <pre v-else class="platform-yaml platform-card" style="min-height: 120px">{{
        form.promptText || '（空）'
      }}</pre>
    </div>
  </div>
</template>
