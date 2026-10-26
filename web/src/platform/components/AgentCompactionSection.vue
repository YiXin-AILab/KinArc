<script setup lang="ts">
import { computed } from 'vue'

import type { AgentFormState } from '../useAgentForm'

const props = defineProps<{ form: AgentFormState }>()

const triggerFill = computed(() => ({
  '--fill': `${((props.form.triggerRatio - 0.5) / 0.45) * 100}%`,
}))
const recentFill = computed(() => ({
  '--fill': `${((props.form.recentMessageRatio - 0.05) / 0.45) * 100}%`,
}))
const ratioOk = computed(() => props.form.recentMessageRatio < props.form.triggerRatio)
</script>

<template>
  <div class="platform-fsec" id="sec-compact">
    <h3>上下文压缩</h3>
    <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 14px">
      <label class="platform-switch">
        <input
          v-model="form.compactionEnabled"
          type="checkbox"
          aria-label="启用压缩"
        /><i />
      </label>
      <span style="font-size: 13.5px"
        >启用 Compactor（触发时产生 ContextCompacted 事件）</span
      >
    </div>
    <template v-if="form.compactionEnabled">
      <div class="platform-frow3">
        <div class="platform-field">
          <label
            >trigger_ratio ·
            <span class="platform-mono" style="color: var(--p-cyan)">{{
              form.triggerRatio.toFixed(2)
            }}</span></label
          >
          <input
            v-model.number="form.triggerRatio"
            type="range"
            min="0.5"
            max="0.95"
            step="0.01"
            :style="triggerFill"
            aria-label="trigger_ratio"
          />
        </div>
        <div class="platform-field">
          <label
            >recent_message_ratio ·
            <span class="platform-mono" style="color: var(--p-cyan)">{{
              form.recentMessageRatio.toFixed(2)
            }}</span></label
          >
          <input
            v-model.number="form.recentMessageRatio"
            type="range"
            min="0.05"
            max="0.5"
            step="0.01"
            :style="recentFill"
            aria-label="recent_message_ratio"
          />
        </div>
        <div class="platform-field">
          <label>safety_margin_tokens</label>
          <input
            v-model.number="form.safetyMarginTokens"
            type="number"
            class="platform-mono"
            aria-label="safety_margin_tokens"
          />
        </div>
      </div>
      <div v-if="ratioOk" class="platform-hint platform-hint-ok">
        ✓ recent_message_ratio（{{ form.recentMessageRatio.toFixed(2) }}）&lt;
        trigger_ratio（{{ form.triggerRatio.toFixed(2) }}），约束满足
      </div>
      <div v-else class="platform-hint">
        ⚠ recent_message_ratio 必须小于 trigger_ratio
      </div>
    </template>
  </div>
</template>
