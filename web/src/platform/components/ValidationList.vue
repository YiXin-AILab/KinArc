<script setup lang="ts">
import type { Issue } from '../../api/platformClient'
import { sectionForField } from '../useAgentForm'

defineProps<{
  errors: Issue[]
  warnings: Issue[]
  checked: boolean
}>()

const emit = defineEmits<{
  locate: [sectionId: string]
}>()
</script>

<template>
  <div style="padding: 16px">
    <div style="display: flex; align-items: center; gap: 9px; margin-bottom: 14px">
      <span class="platform-badge platform-b-amber">dry-run · AgentConfig 校验</span>
    </div>
    <p v-if="!checked" class="platform-mut" style="font-size: 12.5px">
      尚未运行校验。点击顶栏「校验」或保存时自动执行。
    </p>
    <template v-else>
      <div
        v-for="(issue, index) in errors"
        :key="`e-${index}`"
        class="platform-chk platform-chk-err"
      >
        <b>✕ {{ issue.field }}</b>
        <span>{{ issue.message }}</span>
        <a @click="emit('locate', sectionForField(issue.field))">→ 跳转字段</a>
      </div>
      <div
        v-for="(issue, index) in warnings"
        :key="`w-${index}`"
        class="platform-chk platform-chk-warn"
      >
        <b>⚠ {{ issue.field }}</b>
        <span>{{ issue.message }}</span>
        <a @click="emit('locate', sectionForField(issue.field))">→ 跳转字段</a>
      </div>
      <div
        v-if="errors.length === 0 && warnings.length === 0"
        class="platform-chk platform-chk-ok"
      >
        <b>✓ 校验通过</b>
        <span>无错误、无警告</span>
      </div>
    </template>
  </div>
</template>
