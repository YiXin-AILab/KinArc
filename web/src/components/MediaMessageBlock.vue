<script setup lang="ts">
/**
 * 加载需鉴权的资产，并渲染其浏览器本地对象 URL。
 *
 * 仅获取资产来源的公开内容块。props 变化后通过版本号检查丢弃迟到响应；
 * 内容替换和组件卸载会取消请求并撤销对象 URL。
 */

import { computed, onBeforeUnmount, ref, watch } from 'vue'

import type { PublicMediaMessageBlock } from '../domain/types'

const props = defineProps<{
  block: PublicMediaMessageBlock
  readAsset?: (assetId: string, signal?: AbortSignal) => Promise<Blob>
}>()

const objectUrl = ref<string>()
const status = ref<'loading' | 'ready' | 'unavailable' | 'error'>('loading')
const filename = computed(() => props.block.filename?.trim() || props.block.media_type)
let controller: AbortController | undefined
/** 即使旧读取的取消晚于 props 变化完成，也使其结果失效。 */
let generation = 0

watch(
  () => [props.block.asset_id, props.block.source_type, props.readAsset],
  () => void load(),
  { immediate: true },
)

/**
 * 通过注入的鉴权读取器获取资产，并替换当前预览。
 *
 * 来源缺失或不支持时标记为不可用。版本号检查拒绝过期结果；
 * 其他失败只设置错误状态，不向消息泄露请求详情。
 */
async function load(): Promise<void> {
  release()
  const currentGeneration = generation
  const assetId = props.block.asset_id
  if (
    props.block.source_type !== 'asset' ||
    !assetId ||
    props.readAsset === undefined
  ) {
    status.value = 'unavailable'
    return
  }
  controller = new AbortController()
  status.value = 'loading'
  try {
    const blob = await props.readAsset(assetId, controller.signal)
    if (generation !== currentGeneration || controller.signal.aborted) return
    objectUrl.value = URL.createObjectURL(blob)
    status.value = 'ready'
  } catch (error) {
    if (
      generation !== currentGeneration ||
      (error instanceof DOMException && error.name === 'AbortError')
    ) {
      return
    }
    status.value = 'error'
  }
}

/**
 * 使待执行回调失效，取消资产请求并撤销活动对象 URL。
 */
function release(): void {
  generation += 1
  controller?.abort()
  controller = undefined
  if (objectUrl.value !== undefined) {
    URL.revokeObjectURL(objectUrl.value)
    objectUrl.value = undefined
  }
}

/**
 * 在新标签页打开已加载文档，不传递 opener 或 referrer。
 */
function openDocument(): void {
  if (objectUrl.value === undefined) return
  window.open(objectUrl.value, '_blank', 'noopener,noreferrer')
}

onBeforeUnmount(release)
</script>

<template>
  <div class="media-message-block" :data-media-type="block.type">
    <img v-if="block.type === 'image' && objectUrl" :src="objectUrl" :alt="filename" />
    <audio v-else-if="block.type === 'audio' && objectUrl" :src="objectUrl" controls />
    <video v-else-if="block.type === 'video' && objectUrl" :src="objectUrl" controls />
    <button
      v-else-if="block.type === 'document' && objectUrl"
      type="button"
      class="media-document"
      :aria-label="`打开文档 ${filename}`"
      @click="openDocument"
    >
      <span aria-hidden="true">DOC</span>
      <strong>{{ filename }}</strong>
    </button>
    <span
      v-else-if="status === 'loading'"
      class="media-unavailable"
      aria-label="正在读取媒体"
    >
      {{ filename }}
    </span>
    <span
      v-else
      class="media-unavailable"
      :aria-label="status === 'error' ? '媒体读取失败' : '媒体不可预览'"
    >
      {{ filename }}
    </span>
  </div>
</template>
