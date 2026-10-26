import { flushPromises } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'

import type { AssetUploadResponse } from '../domain/types'
import { useAttachmentDrafts } from './useAttachmentDrafts'

function uploaded(
  filename: string,
  mediaType: string,
  assetId: string,
): AssetUploadResponse {
  return {
    asset_id: assetId,
    media_type: mediaType,
    filename,
    size_bytes: 1,
    checksum_sha256: 'a'.repeat(64),
    source: {
      type: 'asset',
      asset_id: assetId,
      media_type: mediaType,
      filename,
    },
  }
}

describe('useAttachmentDrafts', () => {
  it('uploads supported files immediately and preserves block order', async () => {
    const uploadAsset = vi
      .fn()
      .mockResolvedValueOnce(uploaded('first.png', 'image/png', 'asset-1'))
      .mockResolvedValueOnce(uploaded('voice.wav', 'audio/wav', 'asset-2'))
    const drafts = useAttachmentDrafts({
      uploadAsset,
      createObjectURL: () => 'blob:first',
      revokeObjectURL: vi.fn(),
      randomId: () => `local-${uploadAsset.mock.calls.length}`,
    })

    drafts.addFiles([
      new File(['a'], 'first.png', { type: 'image/png' }),
      new File(['b'], 'voice.wav', { type: 'audio/wav' }),
    ])
    expect(drafts.attachments.value.map((item) => item.status)).toEqual([
      'uploading',
      'uploading',
    ])

    await flushPromises()

    expect(uploadAsset).toHaveBeenCalledTimes(2)
    expect(drafts.attachments.value.map((item) => item.status)).toEqual([
      'ready',
      'ready',
    ])
    expect(drafts.readyBlocks.value.map((block) => block.type)).toEqual([
      'image',
      'audio',
    ])
    expect(drafts.hasBlockingAttachment.value).toBe(false)
  })

  it('keeps unsupported MIME as a non-retryable error without uploading', () => {
    const uploadAsset = vi.fn()
    const drafts = useAttachmentDrafts({ uploadAsset })

    drafts.addFiles([new File(['zip'], 'archive.zip', { type: 'application/zip' })])

    expect(uploadAsset).not.toHaveBeenCalled()
    expect(drafts.attachments.value[0]).toMatchObject({
      status: 'error',
      retryable: false,
    })
    expect(drafts.hasBlockingAttachment.value).toBe(true)
  })

  it('isolates upload failure and retries only the failed file', async () => {
    const uploadAsset = vi
      .fn()
      .mockRejectedValueOnce(new Error('network failed'))
      .mockResolvedValueOnce(uploaded('ok.pdf', 'application/pdf', 'asset-ok'))
      .mockResolvedValueOnce(uploaded('bad.png', 'image/png', 'asset-retried'))
    const drafts = useAttachmentDrafts({ uploadAsset })

    drafts.addFiles([
      new File(['a'], 'bad.png', { type: 'image/png' }),
      new File(['b'], 'ok.pdf', { type: 'application/pdf' }),
    ])
    await flushPromises()

    expect(drafts.attachments.value.map((item) => item.status)).toEqual([
      'error',
      'ready',
    ])
    drafts.retry(drafts.attachments.value[0]!.id)
    await flushPromises()

    expect(uploadAsset).toHaveBeenCalledTimes(3)
    expect(drafts.attachments.value.map((item) => item.status)).toEqual([
      'ready',
      'ready',
    ])
    expect(drafts.readyBlocks.value[0]!.source.asset_id).toBe('asset-retried')
  })

  it('aborts an in-flight upload when its attachment is removed', () => {
    let receivedSignal: AbortSignal | undefined
    const uploadAsset = vi.fn((_file: File, signal?: AbortSignal) => {
      receivedSignal = signal
      return new Promise<AssetUploadResponse>(() => undefined)
    })
    const drafts = useAttachmentDrafts({ uploadAsset })
    drafts.addFiles([new File(['a'], 'image.png', { type: 'image/png' })])

    drafts.remove(drafts.attachments.value[0]!.id)

    expect(receivedSignal?.aborted).toBe(true)
    expect(drafts.attachments.value).toEqual([])
  })

  it('revokes image previews on remove, submitted clear, and dispose', async () => {
    const revokeObjectURL = vi.fn()
    let preview = 0
    const drafts = useAttachmentDrafts({
      uploadAsset: vi.fn((file: File) =>
        Promise.resolve(uploaded(file.name, file.type, file.name)),
      ),
      createObjectURL: () => `blob:${++preview}`,
      revokeObjectURL,
    })
    drafts.addFiles([
      new File(['1'], 'one.png', { type: 'image/png' }),
      new File(['2'], 'two.png', { type: 'image/png' }),
      new File(['3'], 'three.png', { type: 'image/png' }),
    ])
    await flushPromises()
    const submittedRevision = drafts.revision.value

    drafts.remove(drafts.attachments.value[0]!.id)
    drafts.clearSubmitted(submittedRevision)
    drafts.addFiles([new File(['4'], 'four.png', { type: 'image/png' })])
    drafts.dispose()

    expect(revokeObjectURL.mock.calls.flat()).toEqual([
      'blob:1',
      'blob:2',
      'blob:3',
      'blob:4',
    ])
  })

  it('clears only attachments included in the submitted revision', async () => {
    const drafts = useAttachmentDrafts({
      uploadAsset: vi.fn((file: File) =>
        Promise.resolve(uploaded(file.name, file.type, file.name)),
      ),
    })
    drafts.addFiles([new File(['1'], 'first.png', { type: 'image/png' })])
    await flushPromises()
    const submittedRevision = drafts.revision.value
    drafts.addFiles([new File(['2'], 'later.png', { type: 'image/png' })])
    await flushPromises()

    drafts.clearSubmitted(submittedRevision)

    expect(drafts.attachments.value.map((item) => item.file.name)).toEqual([
      'later.png',
    ])
  })
})
