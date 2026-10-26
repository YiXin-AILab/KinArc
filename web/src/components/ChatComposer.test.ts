import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { AssetUploadResponse, InputContentBlock } from '../domain/types'
import ChatComposer from './ChatComposer.vue'

enableAutoUnmount(afterEach)

function uploaded(file: File, assetId = 'asset-1'): AssetUploadResponse {
  return {
    asset_id: assetId,
    media_type: file.type,
    filename: file.name,
    size_bytes: file.size,
    checksum_sha256: 'a'.repeat(64),
    source: {
      type: 'asset',
      asset_id: assetId,
      media_type: file.type,
      filename: file.name,
    },
  }
}

describe('ChatComposer', () => {
  it('switches the running action between stop and send as the draft changes', async () => {
    const wrapper = mount(ChatComposer, {
      props: { mode: 'queue', canSteer: true, submitting: false },
    })
    const textarea = wrapper.get('textarea')

    await textarea.trigger('keydown', { key: 'Enter' })
    expect(wrapper.emitted('cancel')).toBeUndefined()
    await wrapper.get('[aria-label="取消运行"]').trigger('click')
    expect(wrapper.emitted('cancel')).toHaveLength(1)

    await textarea.setValue('补充说明')
    expect(wrapper.find('[aria-label="取消运行"]').exists()).toBe(false)
    await wrapper.get('[aria-label="发送消息"]').trigger('click')
    expect(wrapper.emitted('submit')).toEqual([
      ['补充说明', { role: 'user', origin: 'user' }],
    ])
    expect(wrapper.find('[aria-label="取消运行"]').exists()).toBe(true)

    await textarea.setValue('待删除')
    await textarea.setValue('  ')
    expect(wrapper.find('[aria-label="取消运行"]').exists()).toBe(true)
    await wrapper.setProps({ canSteer: false })
    expect(wrapper.find('[aria-label="取消运行"]').exists()).toBe(false)
    expect(wrapper.get('[aria-label="发送消息"]').attributes('disabled')).toBeDefined()
    wrapper.unmount()
  })

  it('keeps attachment drafts in send mode during a run and restores stop on removal', async () => {
    const image = new File(['png'], 'draft.png', { type: 'image/png' })
    const wrapper = mount(ChatComposer, {
      props: {
        mode: 'queue',
        canSteer: true,
        submitting: false,
        uploadAsset: (file: File) => Promise.resolve(uploaded(file)),
      },
    })
    await wrapper.get('textarea').trigger('paste', {
      clipboardData: { files: [image] },
    })
    await flushPromises()
    expect(wrapper.find('[aria-label="取消运行"]').exists()).toBe(false)
    expect(
      wrapper.get('[aria-label="发送消息"]').attributes('disabled'),
    ).toBeUndefined()
    await wrapper.get('[aria-label="移除附件 draft.png"]').trigger('click')
    expect(wrapper.find('[aria-label="取消运行"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('submits trimmed text with Enter and keeps Shift+Enter for a newline', async () => {
    const wrapper = mount(ChatComposer, {
      props: { mode: 'queue', canSteer: false, submitting: false },
    })
    const textarea = wrapper.get('textarea')
    await textarea.setValue('  你好  ')
    await textarea.trigger('keydown', { key: 'Enter', shiftKey: true })
    expect(wrapper.emitted('submit')).toBeUndefined()

    await textarea.trigger('keydown', { key: 'Enter', shiftKey: false })

    expect(wrapper.emitted('submit')).toEqual([
      ['你好', { role: 'user', origin: 'user' }],
    ])
    expect((textarea.element as HTMLTextAreaElement).value).toBe('')
  })

  it('pastes files, uploads immediately, and submits media before text', async () => {
    const image = new File(['png'], 'shot.png', { type: 'image/png' })
    const uploadAsset = vi.fn((file: File) => Promise.resolve(uploaded(file)))
    const submitContent = vi.fn().mockResolvedValue(true)
    const wrapper = mount(ChatComposer, {
      props: {
        mode: 'queue',
        canSteer: false,
        submitting: false,
        uploadAsset,
        submitContent,
      },
    })

    await wrapper.get('textarea').trigger('paste', {
      clipboardData: { files: [image] },
    })
    await flushPromises()
    await wrapper.get('textarea').setValue('描述图片')
    await wrapper.get('[aria-label="发送消息"]').trigger('click')
    await flushPromises()

    expect(uploadAsset).toHaveBeenCalledWith(image, expect.any(AbortSignal))
    const submitted = submitContent.mock.calls[0]![0] as InputContentBlock[]
    expect(submitted.map((block) => block.type)).toEqual(['image', 'text'])
    expect(submitContent.mock.calls[0]![0][1]).toEqual({
      type: 'text',
      text: '描述图片',
    })
    expect(wrapper.find('[data-attachment]').exists()).toBe(false)
  })

  it('selects multiple files and allows a pure attachment message', async () => {
    const document = new File(['pdf'], 'report.pdf', {
      type: 'application/pdf',
    })
    const image = new File(['png'], 'shot.png', { type: 'image/png' })
    const submitContent = vi.fn().mockResolvedValue(true)
    const wrapper = mount(ChatComposer, {
      props: {
        mode: 'queue',
        canSteer: false,
        submitting: false,
        uploadAsset: (file: File) => Promise.resolve(uploaded(file, file.name)),
        submitContent,
      },
    })

    expect(wrapper.get('[aria-label="添加附件"]')).toBeDefined()
    const input = wrapper.get('input[type="file"]')
    Object.defineProperty(input.element, 'files', {
      value: [document, image],
      configurable: true,
    })
    await input.trigger('change')
    await flushPromises()
    await wrapper.get('[aria-label="发送消息"]').trigger('click')
    await flushPromises()

    expect(submitContent.mock.calls[0]![0]).toEqual([
      {
        type: 'document',
        source: expect.objectContaining({ asset_id: 'report.pdf' }),
      },
      {
        type: 'image',
        source: expect.objectContaining({ asset_id: 'shot.png' }),
        detail: 'auto',
      },
    ])
  })

  it('does not intercept ordinary text paste', async () => {
    const wrapper = mount(ChatComposer, {
      props: { mode: 'queue', canSteer: false, submitting: false },
    })
    const preventDefault = vi.fn()

    await wrapper.get('textarea').trigger('paste', {
      clipboardData: { files: [] },
      preventDefault,
    })

    expect(preventDefault).not.toHaveBeenCalled()
  })

  it('shows icon-only independent states and retries a failed upload', async () => {
    const image = new File(['png'], 'retry.png', { type: 'image/png' })
    const uploadAsset = vi
      .fn()
      .mockRejectedValueOnce(new Error('network failed'))
      .mockResolvedValueOnce(uploaded(image))
    const wrapper = mount(ChatComposer, {
      props: {
        mode: 'queue',
        canSteer: false,
        submitting: false,
        uploadAsset,
        submitContent: vi.fn().mockResolvedValue(true),
      },
    })

    await wrapper.get('textarea').trigger('paste', {
      clipboardData: { files: [image] },
    })
    await flushPromises()

    expect(wrapper.get('[aria-label="上传失败，点击重试"]')).toBeDefined()
    expect(wrapper.text()).not.toContain('network failed')
    expect(wrapper.get('[aria-label="发送消息"]').attributes('disabled')).toBeDefined()
    await wrapper.get('[aria-label="上传失败，点击重试"]').trigger('click')
    await flushPromises()

    expect(uploadAsset).toHaveBeenCalledTimes(2)
    expect(wrapper.get('[aria-label="上传完成"]')).toBeDefined()
    expect(
      wrapper.get('[aria-label="发送消息"]').attributes('disabled'),
    ).toBeUndefined()
  })

  it('keeps rejected content and preserves edits made during submission', async () => {
    let resolveSubmission: ((accepted: boolean) => void) | undefined
    const submitContent = vi.fn(
      () =>
        new Promise<boolean>((resolve) => {
          resolveSubmission = resolve
        }),
    )
    const wrapper = mount(ChatComposer, {
      props: {
        mode: 'queue',
        canSteer: false,
        submitting: false,
        submitContent,
      },
    })
    const textarea = wrapper.get('textarea')
    await textarea.setValue('第一条')
    await wrapper.get('[aria-label="发送消息"]').trigger('click')
    expect(textarea.attributes('disabled')).toBeUndefined()
    await textarea.setValue('提交期间新增')
    expect(wrapper.get('[aria-label="发送消息"]').attributes('disabled')).toBeDefined()
    await wrapper.setProps({ submitting: true })
    resolveSubmission?.(true)
    await flushPromises()

    expect((textarea.element as HTMLTextAreaElement).value).toBe('提交期间新增')
    expect(wrapper.get('[aria-label="发送消息"]').attributes('disabled')).toBeDefined()
    await wrapper.setProps({ submitting: false })

    await wrapper.get('[aria-label="发送消息"]').trigger('click')
    await textarea.setValue('失败前继续输入')
    resolveSubmission?.(false)
    await flushPromises()
    expect((textarea.element as HTMLTextAreaElement).value).toBe(
      '提交期间新增\n失败前继续输入',
    )
  })

  it('closes advanced configuration with Escape and an outside click', async () => {
    const wrapper = mount(ChatComposer, {
      attachTo: document.body,
      props: { mode: 'queue', canSteer: false, submitting: false },
    })
    const trigger = wrapper.get('[aria-label="高级消息配置"]')

    await trigger.trigger('click')
    expect(trigger.attributes('aria-expanded')).toBe('true')
    await wrapper.get('.composer-shell').trigger('keydown', { key: 'Escape' })
    expect(trigger.attributes('aria-expanded')).toBe('false')

    await trigger.trigger('click')
    document.body.dispatchEvent(new Event('pointerdown', { bubbles: true }))
    await wrapper.vm.$nextTick()
    expect(trigger.attributes('aria-expanded')).toBe('false')
    wrapper.unmount()
  })

  it('submits and retains advanced values after accepted and rejected sends', async () => {
    const submitContent = vi
      .fn()
      .mockResolvedValueOnce(true)
      .mockResolvedValueOnce(false)
    const wrapper = mount(ChatComposer, {
      props: {
        mode: 'steer',
        canSteer: true,
        submitting: false,
        submitContent,
      },
    })
    const trigger = wrapper.get('[aria-label="高级消息配置"]')
    await trigger.trigger('click')
    const origin = wrapper.get('[aria-label="消息来源"]')
    expect(wrapper.get('[data-role="user"]').attributes('aria-pressed')).toBe('true')
    expect((origin.element as HTMLInputElement).value).toBe('user')
    expect(wrapper.find('[data-advanced-summary]').exists()).toBe(false)

    await wrapper.get('[data-role="system"]').trigger('click')
    expect((origin.element as HTMLInputElement).value).toBe('system')
    await origin.setValue('order.workflow')
    await wrapper.get('[data-role="user"]').trigger('click')
    expect((origin.element as HTMLInputElement).value).toBe('user')
    await origin.setValue('human.console')
    await wrapper.get('[data-role="system"]').trigger('click')
    expect((origin.element as HTMLInputElement).value).toBe('system')
    await wrapper.get('[aria-label="消息来源"]').setValue('order.workflow')
    await trigger.trigger('click')

    expect(wrapper.get('[data-advanced-summary]').text()).toContain('order.workflow')
    const textarea = wrapper.get('textarea')
    await textarea.setValue('退款完成')
    await wrapper.get('[aria-label="发送消息"]').trigger('click')
    await flushPromises()
    expect(submitContent).toHaveBeenNthCalledWith(
      1,
      [{ type: 'text', text: '退款完成' }],
      { role: 'system', origin: 'order.workflow' },
    )

    await textarea.setValue('再次确认')
    await wrapper.get('[aria-label="发送消息"]').trigger('click')
    await flushPromises()
    expect(submitContent).toHaveBeenNthCalledWith(
      2,
      [{ type: 'text', text: '再次确认' }],
      { role: 'system', origin: 'order.workflow' },
    )

    await trigger.trigger('click')
    expect(wrapper.get('[data-role="system"]').attributes('aria-pressed')).toBe('true')
    expect(
      (wrapper.get('[aria-label="消息来源"]').element as HTMLInputElement).value,
    ).toBe('order.workflow')
  })
})
