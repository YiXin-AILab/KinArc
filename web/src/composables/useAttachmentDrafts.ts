/**
 * 独立于已提交的对话消息管理本地附件草稿。
 *
 * 上传以附件 ID 和尝试次数为键，避免过期请求覆盖重试结果。
 * 草稿版本区分提交时捕获的附件和提交期间新加入的文件。
 * 草稿移除或所属作用域销毁时释放对象 URL 和请求。
 */

import { computed, getCurrentScope, onScopeDispose, ref } from 'vue'

import type { AssetSource, AssetUploadResponse, InputMediaBlock } from '../domain/types'

/**
 * 本地上传状态；ready 表示已有可提交的资产引用。
 */
export type AttachmentStatus = 'uploading' | 'ready' | 'error'
/**
 * 附件草稿控制器接受的输入媒体类别。
 */
export type MediaBlockType = 'image' | 'audio' | 'video' | 'document'

/**
 * 本地文件和上传尝试状态，不直接序列化为消息内容。
 *
 * revision 记录草稿加入时的版本，与每次上传的尝试计数不同。
 * previewUrl 由控制器拥有，移除草稿时必须撤销。
 */
export interface DraftAttachment {
  id: string
  file: File
  blockType: MediaBlockType | null
  previewUrl?: string
  status: AttachmentStatus
  retryable: boolean
  source?: AssetSource
  error?: string
  /** 加入版本，用于保留提交快照之后创建的草稿。 */
  revision: number
}

/**
 * 上传和浏览器资源依赖；可注入的辅助函数支持确定性测试。
 */
interface AttachmentDraftOptions {
  uploadAsset(file: File, signal?: AbortSignal): Promise<AssetUploadResponse>
  createObjectURL?: (file: File) => string | undefined
  revokeObjectURL?: (url: string) => void
  randomId?: () => string
}

const DOCUMENT_MEDIA_TYPES = new Set([
  'application/json',
  'application/msword',
  'application/pdf',
  'application/vnd.ms-excel',
  'application/vnd.ms-powerpoint',
  'application/vnd.openxmlformats-officedocument.presentationml.presentation',
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  'text/csv',
  'text/plain',
])

/**
 * 创建附件上传状态和可提交的资产内容块。
 *
 * @param options - 上传适配器及可注入的浏览器 URL/ID 辅助函数。
 * @returns 草稿状态、阻塞或就绪投影，以及草稿生命周期操作。
 * @remarks 在 Vue 作用域内调用时登记清理；否则调用方必须执行 dispose。
 * 原始 File 对象只留在本地，不进入消息载荷。
 */
export function useAttachmentDrafts(options: AttachmentDraftOptions) {
  const attachments = ref<DraftAttachment[]>([])
  const revision = ref(0)
  /** 每个附件的请求所有者；重试或移除只取消对应上传。 */
  const controllers = new Map<string, AbortController>()
  /** 每个附件的尝试计数，使重试或移除后的回调失效。 */
  const attempts = new Map<string, number>()
  const randomId = options.randomId ?? (() => crypto.randomUUID())
  const createObjectURL =
    options.createObjectURL ??
    ((file: File) =>
      typeof URL.createObjectURL === 'function' ? URL.createObjectURL(file) : undefined)
  const revokeObjectURL =
    options.revokeObjectURL ??
    ((url: string) => {
      if (typeof URL.revokeObjectURL === 'function') URL.revokeObjectURL(url)
    })

  /** 提交载荷仅包含成功上传且已分类的资产来源。 */
  const readyBlocks = computed<InputMediaBlock[]>(() =>
    attachments.value.flatMap((attachment) => {
      if (
        attachment.status !== 'ready' ||
        attachment.blockType === null ||
        attachment.source === undefined
      ) {
        return []
      }
      return [mediaBlock(attachment.blockType, attachment.source)]
    }),
  )
  /** 上传中或失败的草稿会阻止提交，直至处理完成或移除。 */
  const hasBlockingAttachment = computed(() =>
    attachments.value.some((attachment) => attachment.status !== 'ready'),
  )

  /**
   * 按顺序加入选中的文件，并为受支持媒体类型启动上传。
   */
  function addFiles(files: Iterable<File>): void {
    for (const file of files) addFile(file)
  }

  /**
   * 分配草稿版本、创建图片预览，并在本地拒绝不支持的 MIME 类型。
   */
  function addFile(file: File): void {
    const blockType = mediaBlockType(file.type)
    const nextRevision = ++revision.value
    const attachment: DraftAttachment = {
      id: randomId(),
      file,
      blockType,
      status: blockType === null ? 'error' : 'uploading',
      retryable: blockType !== null,
      error: blockType === null ? '不支持的文件类型' : undefined,
      previewUrl: blockType === 'image' ? createObjectURL(file) : undefined,
      revision: nextRevision,
    }
    attachments.value.push(attachment)
    if (blockType !== null) startUpload(attachment)
  }

  /**
   * 上传前替换附件请求并递增尝试标记。
   *
   * currentAttempt 忽略过期结果；重试前先清除旧来源和错误。
   */
  function startUpload(attachment: DraftAttachment): void {
    controllers.get(attachment.id)?.abort()
    const controller = new AbortController()
    const attempt = (attempts.get(attachment.id) ?? 0) + 1
    controllers.set(attachment.id, controller)
    attempts.set(attachment.id, attempt)
    attachment.status = 'uploading'
    attachment.retryable = true
    attachment.error = undefined
    attachment.source = undefined
    void options
      .uploadAsset(attachment.file, controller.signal)
      .then((result) => finishUpload(attachment.id, attempt, result.source))
      .catch((error: unknown) => failUpload(attachment.id, attempt, error))
  }

  /**
   * 仅将当前上传尝试标记为就绪，并保存服务端资产引用。
   */
  function finishUpload(id: string, attempt: number, source: AssetSource): void {
    const attachment = currentAttempt(id, attempt)
    if (attachment === undefined) return
    controllers.delete(id)
    attachment.source = source
    attachment.status = 'ready'
    attachment.error = undefined
  }

  /**
   * 仅展示当前尝试的失败，并允许重试上传。
   */
  function failUpload(id: string, attempt: number, error: unknown): void {
    const attachment = currentAttempt(id, attempt)
    if (attachment === undefined) return
    controllers.delete(id)
    attachment.status = 'error'
    attachment.retryable = true
    attachment.error = error instanceof Error ? error.message : '上传失败'
  }

  /**
   * 仅在草稿仍存在且尝试标记匹配时返回草稿。
   */
  function currentAttempt(id: string, attempt: number): DraftAttachment | undefined {
    if (attempts.get(id) !== attempt) return undefined
    return attachments.value.find((attachment) => attachment.id === id)
  }

  /**
   * 重新上传失败但可重试的受支持附件；忽略其他 ID 或状态。
   */
  function retry(id: string): void {
    const attachment = attachments.value.find((item) => item.id === id)
    if (
      attachment === undefined ||
      attachment.status !== 'error' ||
      !attachment.retryable ||
      attachment.blockType === null
    ) {
      return
    }
    startUpload(attachment)
  }

  /**
   * 移除一份本地草稿，取消上传并撤销预览 URL。
   */
  function remove(id: string): void {
    const index = attachments.value.findIndex((item) => item.id === id)
    if (index < 0) return
    const [attachment] = attachments.value.splice(index, 1)
    if (attachment !== undefined) release(attachment)
  }

  /**
   * 仅释放成功提交时捕获的草稿。
   *
   * @param submittedRevision - 发送请求开始前记录的版本。
   * @remarks 请求期间加入的草稿版本更高，会继续保留。
   */
  function clearSubmitted(submittedRevision: number): void {
    const retained: DraftAttachment[] = []
    for (const attachment of attachments.value) {
      if (attachment.revision <= submittedRevision) release(attachment)
      else retained.push(attachment)
    }
    attachments.value = retained
  }

  /**
   * 取消草稿请求，使回调失效并释放预览对象 URL。
   */
  function release(attachment: DraftAttachment): void {
    controllers.get(attachment.id)?.abort()
    controllers.delete(attachment.id)
    attempts.delete(attachment.id)
    if (attachment.previewUrl !== undefined) {
      revokeObjectURL(attachment.previewUrl)
    }
  }

  /**
   * 释放全部草稿资源并清空列表；已清理后再次调用仍安全。
   */
  function dispose(): void {
    for (const attachment of attachments.value) release(attachment)
    attachments.value = []
  }

  if (getCurrentScope() !== undefined) onScopeDispose(dispose)

  return {
    attachments,
    revision,
    readyBlocks,
    hasBlockingAttachment,
    addFiles,
    retry,
    remove,
    clearSubmitted,
    dispose,
  }
}

/**
 * 将 MIME 类型归类为受支持的输入块；不支持时返回 null。
 */
function mediaBlockType(mediaType: string): MediaBlockType | null {
  const normalized = mediaType.toLowerCase()
  if (normalized.startsWith('image/')) return 'image'
  if (normalized.startsWith('audio/')) return 'audio'
  if (normalized.startsWith('video/')) return 'video'
  if (DOCUMENT_MEDIA_TYPES.has(normalized)) return 'document'
  return null
}

/**
 * 构建资产输入块；图片输入指定 detail=auto。
 */
function mediaBlock(type: MediaBlockType, source: AssetSource): InputMediaBlock {
  if (type === 'image') return { type, source, detail: 'auto' }
  return { type, source }
}
