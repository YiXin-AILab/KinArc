<script setup lang="ts">
/**
 * 按现有顺序渲染附件草稿，并转发条目操作。
 *
 * 列表仅负责展示；上传状态和对象 URL 清理由草稿控制器负责。
 * 布局和横向滚动在输入区样式中定义。
 */

import type { DraftAttachment } from '../composables/useAttachmentDrafts'
import AttachmentDraftItem from './AttachmentDraftItem.vue'

defineProps<{ attachments: DraftAttachment[] }>()

const emit = defineEmits<{
  retry: [id: string]
  remove: [id: string]
}>()
</script>

<template>
  <div v-if="attachments.length" class="attachment-draft-list" aria-label="附件">
    <AttachmentDraftItem
      v-for="attachment in attachments"
      :key="attachment.id"
      :attachment="attachment"
      @retry="emit('retry', $event)"
      @remove="emit('remove', $event)"
    />
  </div>
</template>
