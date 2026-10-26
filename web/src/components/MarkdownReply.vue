<script setup lang="ts">
/** 使用同一配置渲染实时及历史助手正文。 */
import { Markdown, type ControlsConfig, type CdnOptions } from 'vue-stream-markdown'
import 'vue-stream-markdown/index.css'
import 'vue-stream-markdown/theme.css'

defineProps<{ content: string; streaming: boolean }>()

// 代码渲染只在本地进行，仅开放当前对话界面需要的控件。
const cdnOptions: CdnOptions = {
  shiki: false,
  mermaid: false,
  beautifulMermaid: false,
  katex: false,
}
const controls: ControlsConfig = {
  code: { copy: true, download: false, fullscreen: false, collapse: false },
  table: false,
  image: false,
  mermaid: false,
}
</script>

<template>
  <Markdown
    class="assistant-markdown"
    :content="content"
    :mode="streaming ? 'streaming' : 'static'"
    :is-dark="true"
    :enable-animate="false"
    :cdn-options="cdnOptions"
    :previewers="false"
    :controls="controls"
    :code-options="{ languageIcon: false, lineNumbers: false }"
    locale="zh-CN"
  />
</template>
