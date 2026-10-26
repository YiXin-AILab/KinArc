<script setup lang="ts">
/**
 * 收集提交到选中会话的文本、附件草稿和元数据。
 *
 * 仅在运行活跃且草稿为空时，共用操作按钮显示停止。
 * 提交时捕获附件版本；失败时恢复文本而不覆盖新输入。
 * 此组件发出操作意图，并将后端操作交给父组件。
 */

import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'

import { useAttachmentDrafts } from '../composables/useAttachmentDrafts'
import type {
  AssetUploadResponse,
  InboundRole,
  InputContentBlock,
  MessageSubmissionOptions,
  SubmissionMode,
} from '../domain/types'
import AttachmentDraftList from './AttachmentDraftList.vue'

/**
 * 父组件控制的执行状态，以及可选的上传和提交适配器。
 * submitContent 报告接受结果；submit 事件是纯文本的替代路径。
 */
const props = defineProps<{
  mode: SubmissionMode
  canSteer: boolean
  submitting: boolean
  uploadAsset?: (file: File, signal?: AbortSignal) => Promise<AssetUploadResponse>
  submitContent?: (
    content: InputContentBlock[],
    options: MessageSubmissionOptions,
  ) => Promise<boolean>
}>()

/** 用户意图：cancel 针对当前运行，update:mode 改变后续输入模式。 */
const emit = defineEmits<{
  submit: [content: string, options: MessageSubmissionOptions]
  cancel: []
  'update:mode': [mode: SubmissionMode]
}>()

const content = ref('')
const composerShell = ref<HTMLElement | null>(null)
const textarea = ref<HTMLTextAreaElement | null>(null)

/** 允许工作台导航恢复输入焦点，不暴露输入元素。 */
function focus(): void {
  textarea.value?.focus()
}

defineExpose({ focus })
const fileInput = ref<HTMLInputElement | null>(null)
const localSubmitting = ref(false)
const isAdvancedOpen = ref(false)
const role = ref<InboundRole>('user')
const origin = ref('user')
const drafts = useAttachmentDrafts({
  uploadAsset: (file, signal) => {
    if (props.uploadAsset === undefined) {
      return Promise.reject(new Error('附件上传不可用'))
    }
    return props.uploadAsset(file, signal)
  },
})
/** 阻止并发提交，要求内容非空且所有附件就绪。 */
const isSubmitDisabled = computed(() => {
  const hasContent = content.value.trim().length > 0
  const hasAttachments = drafts.attachments.value.length > 0
  return (
    props.submitting ||
    localSubmitting.value ||
    drafts.hasBlockingAttachment.value ||
    (!hasContent && !hasAttachments)
  )
})
/** 草稿内容优先于停止操作，使用户能在运行期间提交输入。 */
const showStop = computed(
  () =>
    props.canSteer && !content.value.trim() && drafts.attachments.value.length === 0,
)
const hasAdvancedValues = computed(
  () => role.value !== 'user' || origin.value !== 'user',
)
const advancedSummary = computed(
  () => `${role.value.toUpperCase()} ·${origin.value ? ` ${origin.value}` : '（空）'}`,
)

onMounted(() => document.addEventListener('pointerdown', onDocumentPointerDown))
onBeforeUnmount(() =>
  document.removeEventListener('pointerdown', onDocumentPointerDown),
)

/**
 * 捕获并提交草稿，再根据接受结果对齐文本和附件。
 *
 * 异步 submitContent 路径仅在获接受后移除捕获的附件，
 * 并将被拒绝的文本恢复到新输入文本之前。可选的纯文本事件路径同步发出事件，
 * 不等待接受确认便清空文本。Enter 不会取消运行。
 */
async function submit(): Promise<void> {
  const value = content.value.trim()
  if (isSubmitDisabled.value) return
  const options: MessageSubmissionOptions = {
    role: role.value,
    origin: origin.value,
  }
  if (props.submitContent === undefined) {
    if (!value) return
    emit('submit', value, options)
    content.value = ''
    await nextTick(resize)
    return
  }
  const blocks: InputContentBlock[] = [...drafts.readyBlocks.value]
  if (value) blocks.push({ type: 'text', text: value })
  const submittedRevision = drafts.revision.value
  content.value = ''
  localSubmitting.value = true
  await nextTick(resize)
  let accepted = false
  try {
    accepted = await props.submitContent(blocks, options)
  } catch {
    accepted = false
  } finally {
    localSubmitting.value = false
  }
  if (accepted) {
    drafts.clearSubmitted(submittedRevision)
  } else if (value) {
    content.value = content.value.trim() ? `${value}\n${content.value}` : value
    await nextTick(resize)
  }
}

/**
 * 设置选中的输入角色，并将来源重置为对应默认值。
 */
function selectRole(nextRole: InboundRole): void {
  role.value = nextRole
  origin.value = nextRole
}

/**
 * 切换角色及来源弹出层，不改变提交元数据。
 */
function toggleAdvanced(): void {
  isAdvancedOpen.value = !isAdvancedOpen.value
}

/**
 * 关闭高级元数据控件，同时保留已选值。
 */
function closeAdvanced(): void {
  isAdvancedOpen.value = false
}

/**
 * 仅当指针事件发生在输入区外时关闭高级控件。
 */
function onDocumentPointerDown(event: PointerEvent): void {
  const target = event.target
  if (
    !isAdvancedOpen.value ||
    !(target instanceof Node) ||
    composerShell.value?.contains(target)
  ) {
    return
  }
  closeAdvanced()
}

/**
 * 普通 Enter 发送；保留 Shift+Enter 和输入法组合输入行为。
 */
function onKeydown(event: KeyboardEvent): void {
  if (event.key !== 'Enter' || event.shiftKey || event.isComposing) return
  event.preventDefault()
  void submit()
}

/**
 * 将剪贴板文件转为草稿；仅存在文件时阻止默认粘贴。
 */
function onPaste(event: ClipboardEvent): void {
  const files = Array.from(event.clipboardData?.files ?? [])
  if (files.length === 0) return
  event.preventDefault()
  drafts.addFiles(files)
}

/**
 * 将选中文件加入队列，并重置输入框以允许再次选择同一文件。
 */
function onFilesSelected(event: Event): void {
  const input = event.target as HTMLInputElement
  drafts.addFiles(Array.from(input.files ?? []))
  input.value = ''
}

/**
 * 按内容调整文本框高度，最高 180 CSS 像素；元素未挂载时忽略。
 */
function resize(): void {
  const element = textarea.value
  if (!element) return
  element.style.height = 'auto'
  element.style.height = `${Math.min(element.scrollHeight, 180)}px`
}
</script>

<template>
  <AttachmentDraftList
    :attachments="drafts.attachments.value"
    @retry="drafts.retry"
    @remove="drafts.remove"
  />
  <div ref="composerShell" class="composer-shell" @keydown.esc="closeAdvanced">
    <div class="composer-toolbar">
      <div class="composer-toolbar-main">
        <button
          v-if="uploadAsset"
          class="attachment-button"
          type="button"
          aria-label="添加附件"
          title="添加附件"
          @click="fileInput?.click()"
        >
          <svg viewBox="0 0 20 20" aria-hidden="true">
            <path d="m7 10 5-5a3 3 0 0 1 4 4l-7 7a5 5 0 0 1-7-7l7-7" />
          </svg>
        </button>
        <div class="composer-advanced">
          <button
            type="button"
            class="composer-advanced-trigger"
            :class="{ active: hasAdvancedValues }"
            aria-label="高级消息配置"
            :aria-expanded="isAdvancedOpen"
            aria-controls="composer-advanced-panel"
            @click="toggleAdvanced"
          >
            <svg viewBox="0 0 20 20" aria-hidden="true">
              <circle cx="10" cy="10" r="2.5" />
              <path
                d="M10 3v2M10 15v2M3 10h2M15 10h2M5 5l1.4 1.4M13.6 13.6 15 15M15 5l-1.4 1.4M6.4 13.6 5 15"
              />
            </svg>
            <span
              v-if="hasAdvancedValues"
              data-advanced-summary
              class="composer-advanced-summary"
              :title="advancedSummary"
            >
              {{ advancedSummary }}
            </span>
          </button>
          <div
            v-if="isAdvancedOpen"
            id="composer-advanced-panel"
            class="composer-advanced-panel"
            role="dialog"
            aria-label="消息注入配置"
          >
            <div class="composer-advanced-field">
              <span>角色</span>
              <div class="composer-role-selector" aria-label="消息角色">
                <button
                  type="button"
                  data-role="user"
                  :aria-pressed="role === 'user'"
                  :class="{ active: role === 'user' }"
                  @click="selectRole('user')"
                >
                  USER
                </button>
                <button
                  type="button"
                  data-role="system"
                  :aria-pressed="role === 'system'"
                  :class="{ active: role === 'system' }"
                  @click="selectRole('system')"
                >
                  SYSTEM
                </button>
              </div>
            </div>
            <label class="composer-advanced-field">
              <span>来源</span>
              <input
                v-model="origin"
                class="composer-origin-input"
                aria-label="消息来源"
                autocomplete="off"
              />
            </label>
          </div>
        </div>
        <div class="composer-mode" aria-label="消息提交模式">
          <button
            type="button"
            data-mode="queue"
            :class="{ active: mode === 'queue' }"
            :aria-pressed="mode === 'queue'"
            @click="emit('update:mode', 'queue')"
          >
            <svg viewBox="0 0 20 20" aria-hidden="true">
              <path d="M4 5h12M4 10h8M4 15h5" />
            </svg>
            下一轮执行
          </button>
          <button
            type="button"
            data-mode="steer"
            :class="{ active: mode === 'steer' }"
            :aria-pressed="mode === 'steer'"
            @click="emit('update:mode', 'steer')"
          >
            <svg viewBox="0 0 20 20" aria-hidden="true">
              <path d="m4 14 5-9 2 5 5-3-4 8-3-4-5 3Z" />
            </svg>
            立即引导
          </button>
        </div>
      </div>
    </div>

    <div class="composer-input">
      <input
        ref="fileInput"
        class="visually-hidden"
        type="file"
        multiple
        accept="image/*,audio/*,video/*,.json,.pdf,.doc,.docx,.xls,.xlsx,.ppt,.pptx,.csv,.txt"
        tabindex="-1"
        @change="onFilesSelected"
      />
      <textarea
        ref="textarea"
        v-model="content"
        rows="1"
        aria-label="消息内容"
        @input="resize"
        @keydown="onKeydown"
        @paste="onPaste"
      />
      <button
        class="send-button"
        type="button"
        :disabled="showStop ? submitting || localSubmitting : isSubmitDisabled"
        :aria-label="showStop ? '取消运行' : '发送消息'"
        :title="showStop ? '停止当前运行' : '发送消息'"
        @click="showStop ? emit('cancel') : submit()"
      >
        <span v-if="showStop" class="stop-icon" aria-hidden="true" />
        <svg v-else viewBox="0 0 24 24" aria-hidden="true">
          <path d="m5 12 14-7-4 14-3-6-7-1Z" />
          <path d="m12 13 7-8" />
        </svg>
      </button>
    </div>
    <p class="composer-hint">
      <span v-if="submitting">正在提交…</span>
      <span class="composer-shortcut">Enter 发送 · Shift + Enter 换行</span>
    </p>
  </div>
</template>
