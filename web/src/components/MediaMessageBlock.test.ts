import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { PublicMediaMessageBlock } from '../domain/types'
import MediaMessageBlock from './MediaMessageBlock.vue'

enableAutoUnmount(afterEach)

const NativeURL = URL
const createObjectURL = vi.fn<(blob: Blob) => string>()
const revokeObjectURL = vi.fn<(url: string) => void>()

beforeEach(() => {
  createObjectURL.mockReset()
  revokeObjectURL.mockReset()
  vi.stubGlobal(
    'URL',
    class extends NativeURL {
      static createObjectURL = createObjectURL
      static revokeObjectURL = revokeObjectURL
    },
  )
})

function media(
  type: PublicMediaMessageBlock['type'],
  assetId: string | null = 'asset-1',
): PublicMediaMessageBlock {
  return {
    type,
    source_type: assetId ? 'asset' : 'url',
    media_type: type === 'document' ? 'application/pdf' : `${type}/test`,
    filename: type === 'document' ? 'report.pdf' : `${type}.bin`,
    asset_id: assetId,
  } as PublicMediaMessageBlock
}

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('MediaMessageBlock', () => {
  it('reads an image asset and revokes its Object URL on unmount', async () => {
    const readAsset = vi
      .fn()
      .mockResolvedValue(new Blob(['image'], { type: 'image/png' }))
    createObjectURL.mockReturnValue('blob:asset-1')

    const wrapper = mount(MediaMessageBlock, {
      props: { block: media('image'), readAsset },
    })
    await flushPromises()

    expect(readAsset).toHaveBeenCalledWith('asset-1', expect.any(AbortSignal))
    expect(wrapper.get('img').attributes('src')).toBe('blob:asset-1')
    wrapper.unmount()
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:asset-1')
  })

  it.each([
    ['audio', 'audio'],
    ['video', 'video'],
  ] as const)('renders %s with native controls', async (type, selector) => {
    createObjectURL.mockReturnValue(`blob:${type}`)
    const wrapper = mount(MediaMessageBlock, {
      props: {
        block: media(type),
        readAsset: vi.fn().mockResolvedValue(new Blob([type])),
      },
    })
    await flushPromises()

    expect(wrapper.get(selector).attributes('controls')).toBeDefined()
    expect(wrapper.get(selector).attributes('src')).toBe(`blob:${type}`)
  })

  it('opens a complete document Blob in a new tab', async () => {
    createObjectURL.mockReturnValue('blob:document')
    const open = vi.spyOn(window, 'open').mockImplementation(() => null)
    const wrapper = mount(MediaMessageBlock, {
      props: {
        block: media('document'),
        readAsset: vi.fn().mockResolvedValue(new Blob(['pdf'])),
      },
    })
    await flushPromises()

    await wrapper.get('[aria-label="打开文档 report.pdf"]').trigger('click')
    expect(open).toHaveBeenCalledWith('blob:document', '_blank', 'noopener,noreferrer')
  })

  it('does not read redacted non-Asset media', async () => {
    const readAsset = vi.fn()
    const wrapper = mount(MediaMessageBlock, {
      props: { block: media('image', null), readAsset },
    })
    await flushPromises()

    expect(readAsset).not.toHaveBeenCalled()
    expect(wrapper.get('[aria-label="媒体不可预览"]')).toBeDefined()
  })

  it('shows an unavailable card when authenticated reading fails', async () => {
    const wrapper = mount(MediaMessageBlock, {
      props: {
        block: media('video'),
        readAsset: vi.fn().mockRejectedValue(new Error('forbidden')),
      },
    })
    await flushPromises()

    expect(wrapper.get('[aria-label="媒体读取失败"]')).toBeDefined()
    expect(wrapper.text()).not.toContain('forbidden')
  })

  it('aborts the previous read when the asset changes', async () => {
    const signals: AbortSignal[] = []
    const readAsset = vi.fn((_assetId: string, signal?: AbortSignal) => {
      if (signal) signals.push(signal)
      return new Promise<Blob>(() => undefined)
    })
    const wrapper = mount(MediaMessageBlock, {
      props: { block: media('image', 'asset-1'), readAsset },
    })
    await wrapper.setProps({ block: media('image', 'asset-2') })

    expect(signals[0]?.aborted).toBe(true)
    wrapper.unmount()
    expect(signals[1]?.aborted).toBe(true)
  })
})
