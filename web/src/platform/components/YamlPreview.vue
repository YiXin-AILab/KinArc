<script setup lang="ts">
import { computed } from 'vue'

import { highlightYaml } from '../yaml'

const props = defineProps<{ yaml: string }>()

const html = computed(() => highlightYaml(props.yaml))
</script>

<template>
  <!-- 高亮 HTML 由 highlightYaml 转义后生成，不含用户注入的标记 -->
  <pre class="platform-yaml" v-html="html" />
</template>
