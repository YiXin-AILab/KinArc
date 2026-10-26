<script setup lang="ts">
/**
 * 展示已接受但尚未进入权威消息历史的输入。
 *
 * 每条输入保留其 queue 或 steer 模式。此列表只提供摘要，
 * 不重新提交消息，也不决定后端何时领取。
 */

import type { PendingInput } from '../domain/types'

defineProps<{ inputs: PendingInput[] }>()

/**
 * 优先显示输入文本，其次显示附件文件名或 MIME 类型，最后使用通用附件标签。
 */
function summary(input: PendingInput): string {
  if (input.content.trim()) return input.content
  const media = input.blocks.find((block) => block.type !== 'text')
  return media?.filename?.trim() || media?.media_type || '附件消息'
}
</script>

<template>
  <div v-if="inputs.length" class="pending-input-list" aria-label="待执行消息">
    <div
      v-for="input in inputs"
      :key="input.messageId"
      data-pending-input
      class="pending-input"
    >
      <span :data-mode="input.mode">{{ input.mode.toUpperCase() }}</span>
      <p>{{ summary(input) }}</p>
    </div>
  </div>
</template>
