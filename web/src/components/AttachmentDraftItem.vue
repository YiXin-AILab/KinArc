<script setup lang="ts">
/**
 * 展示单个附件的预览、上传状态和可用操作。
 *
 * 接收 useAttachmentDrafts 拥有的草稿，并在重试或移除时发出其 ID；
 * 不启动上传，也不负责预览对象的生命周期。
 */

import type { DraftAttachment } from '../composables/useAttachmentDrafts'

defineProps<{ attachment: DraftAttachment }>()

const emit = defineEmits<{
  retry: [id: string]
  remove: [id: string]
}>()
</script>

<template>
  <article class="attachment-draft" data-attachment>
    <img
      v-if="attachment.previewUrl"
      class="attachment-thumbnail"
      :src="attachment.previewUrl"
      :alt="attachment.file.name"
    />
    <span v-else class="attachment-type" aria-hidden="true">
      {{ attachment.blockType?.slice(0, 3).toUpperCase() ?? 'FILE' }}
    </span>
    <span class="attachment-name" :title="attachment.file.name">
      {{ attachment.file.name }}
    </span>
    <span
      v-if="attachment.status === 'uploading'"
      class="attachment-status attachment-spinner"
      aria-label="正在上传"
      title="正在上传"
    />
    <span
      v-else-if="attachment.status === 'ready'"
      class="attachment-status attachment-ready"
      aria-label="上传完成"
      title="上传完成"
    >
      <i class="attachment-result-dot" aria-hidden="true" />
    </span>
    <button
      v-else-if="attachment.retryable"
      type="button"
      class="attachment-status attachment-error"
      aria-label="上传失败，点击重试"
      :title="attachment.error ?? '上传失败，点击重试'"
      @click="emit('retry', attachment.id)"
    >
      <i class="attachment-result-dot" aria-hidden="true" />
    </button>
    <span
      v-else
      class="attachment-status attachment-error"
      aria-label="文件类型不受支持"
      :title="attachment.error"
    >
      <i class="attachment-result-dot" aria-hidden="true" />
    </span>
    <button
      type="button"
      class="attachment-remove"
      :aria-label="`移除附件 ${attachment.file.name}`"
      @click="emit('remove', attachment.id)"
    >
      ×
    </button>
  </article>
</template>
