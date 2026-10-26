<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'

import { createRuntimeClient } from '../api/client'
import ChatWorkspace from '../components/ChatWorkspace.vue'
import type { AgentDefinition } from '../config'
import { loadRuntimeConfig } from '../config'

const config = loadRuntimeConfig(import.meta.env)
const runtimeClient = createRuntimeClient({
  apiBaseUrl: config.apiBaseUrl,
  appId: config.appId,
  userId: config.userId,
  tokenProvider: () => sessionStorage.getItem('agentsystem.auth-token'),
})
const route = useRoute()
const agents = ref<AgentDefinition[] | null>(null)
const error = ref<string | null>(null)
const isLoading = ref(false)
const runtimeAvailable = ref(false)
const availabilityController = new AbortController()
let availabilityTimer: ReturnType<typeof setTimeout> | undefined

const initialAgentId = computed(() =>
  typeof route.query.agent === 'string' ? route.query.agent : undefined,
)

/**
 * 检查完成 15 秒后安排下一次检查；页面卸载取消后不再安排。
 */
function scheduleAvailabilityCheck(): void {
  clearTimeout(availabilityTimer)
  if (!availabilityController.signal.aborted) {
    availabilityTimer = setTimeout(() => void checkAvailability(), 15000)
  }
}

/**
 * 以五秒超时探测 Agent 目录，仅更新服务可用状态。
 */
async function checkAvailability(): Promise<void> {
  try {
    await runtimeClient.agents(
      AbortSignal.any([availabilityController.signal, AbortSignal.timeout(5000)]),
    )
    runtimeAvailable.value = true
  } catch {
    runtimeAvailable.value = false
  } finally {
    scheduleAvailabilityCheck()
  }
}

async function loadAgents(): Promise<void> {
  isLoading.value = true
  error.value = null
  agents.value = null
  try {
    agents.value = await runtimeClient.agents(
      AbortSignal.any([availabilityController.signal, AbortSignal.timeout(5000)]),
    )
    runtimeAvailable.value = true
    scheduleAvailabilityCheck()
  } catch (caught) {
    runtimeAvailable.value = false
    error.value = caught instanceof Error ? caught.message : 'Agent 目录加载失败'
  } finally {
    isLoading.value = false
  }
}

onMounted(() => void loadAgents())
onBeforeUnmount(() => {
  availabilityController.abort()
  clearTimeout(availabilityTimer)
})
</script>

<template>
  <ChatWorkspace
    v-if="agents?.length"
    :config="config"
    :client="runtimeClient"
    :agents="agents"
    :runtime-available="runtimeAvailable"
    :initial-agent-id="initialAgentId"
  />
  <main v-else class="catalog-state">
    <p v-if="isLoading">正在加载 Agent…</p>
    <template v-else-if="error">
      <p role="alert">{{ error }}</p>
      <button type="button" @click="loadAgents">重试</button>
    </template>
    <p v-else>暂无可用 Agent</p>
  </main>
</template>
