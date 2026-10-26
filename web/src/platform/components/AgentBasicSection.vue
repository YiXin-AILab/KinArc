<script setup lang="ts">
import type { AgentFormState } from '../useAgentForm'

defineProps<{
  form: AgentFormState
  /** 编辑页为 true（创建后锁定）；向导第 1 步为 false。 */
  lockAgentId?: boolean
}>()
</script>

<template>
  <div class="platform-fsec" id="sec-basic">
    <h3>基本信息</h3>
    <div class="platform-frow2">
      <div class="platform-field">
        <label>
          agent_id
          <span
            v-if="lockAgentId"
            class="platform-badge platform-b-mut"
            style="margin-left: 4px"
            >创建后锁定</span
          >
        </label>
        <input
          v-model="form.agentId"
          type="text"
          class="platform-mono"
          :disabled="lockAgentId"
          placeholder="loan-advisor"
          aria-label="agent_id"
        />
      </div>
      <div class="platform-field">
        <label>agent_name</label>
        <input
          v-model="form.agentName"
          type="text"
          placeholder="loan_advisor"
          aria-label="agent_name"
        />
      </div>
    </div>
  </div>
</template>
