<script setup lang="ts">
import { computed } from 'vue'

import type { AgentFormState } from '../useAgentForm'

const props = defineProps<{ form: AgentFormState }>()

const extraJson = computed(() => JSON.stringify(props.form.extraConfig, null, 2))
const hasExtra = computed(() => Object.keys(props.form.extraConfig).length > 0)
</script>

<template>
  <div class="platform-fsec" id="sec-adv">
    <h3
      >高级
      <span class="platform-mut" style="font-size: 12px; font-weight: 400"
        >· 面向开发者</span
      ></h3
    >
    <div class="platform-field">
      <label>metadata · JSON</label>
      <textarea
        v-model="form.metadataText"
        rows="3"
        class="platform-mono"
        placeholder='{ "team": "banking" }'
        aria-label="metadata"
      />
    </div>
    <div class="platform-field">
      <label>plugin_options · JSON</label>
      <textarea
        v-model="form.pluginOptionsText"
        rows="3"
        class="platform-mono"
        placeholder='{ "discoverable": { "auto_unlock": false } }'
        aria-label="plugin_options"
      />
    </div>
    <div v-if="hasExtra" class="platform-field">
      <label>
        未识别字段
        <span class="platform-badge platform-b-amber" style="margin-left: 4px"
          >只读</span
        >
      </label>
      <pre class="platform-yaml platform-card">{{ extraJson }}</pre>
      <div class="platform-hint" style="color: var(--p-mut)">
        以上字段来自现有 config，保存时原样写回，不会因表单编辑丢失
      </div>
    </div>
  </div>
</template>
