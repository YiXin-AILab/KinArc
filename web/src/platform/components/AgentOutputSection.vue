<script setup lang="ts">
import type { Catalog } from '../../api/platformClient'
import type { AgentFormState } from '../useAgentForm'

defineProps<{
  form: AgentFormState
  catalog: Catalog | null
}>()
</script>

<template>
  <div class="platform-fsec" id="sec-out">
    <h3>输出</h3>
    <div class="platform-frow2">
      <div class="platform-field">
        <label>output_mode</label>
        <div style="display: flex; gap: 10px">
          <label
            class="platform-radiocard"
            :class="{ on: form.outputMode === 'stream' }"
          >
            <input v-model="form.outputMode" type="radio" value="stream" />
            <b>stream</b><span class="platform-mut">SSE 增量输出</span>
          </label>
          <label
            class="platform-radiocard"
            :class="{ on: form.outputMode === 'complete' }"
          >
            <input v-model="form.outputMode" type="radio" value="complete" />
            <b>complete</b><span class="platform-mut">终态一次性返回</span>
          </label>
        </div>
      </div>
      <div class="platform-field">
        <label>outbound · PublicProjector</label>
        <select v-model="form.outboundType" aria-label="outbound">
          <option
            v-for="outbound in catalog?.outbounds ?? [{ type: 'stream' }]"
            :key="outbound.type"
            :value="outbound.type"
          >
            {{ outbound.type }}
          </option>
          <option
            v-if="
              form.outboundType &&
              !(catalog?.outbounds ?? []).some((o) => o.type === form.outboundType)
            "
            :value="form.outboundType"
          >
            {{ form.outboundType }}（未注册）
          </option>
        </select>
        <div class="platform-hint" style="color: var(--p-mut)"
          >具体实现由代码注册 Factory，此处按名称选择</div
        >
      </div>
    </div>
    <div class="platform-field">
      <label>outbound.options · JSON</label>
      <textarea
        v-model="form.outboundOptionsText"
        rows="2"
        class="platform-mono"
        placeholder='{ "include_thinking": true }'
        aria-label="outbound options"
      />
    </div>
  </div>
</template>
