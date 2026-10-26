<script setup lang="ts">
/**
 * 连接对话控制器、展示组件和浏览器存储。
 *
 * 管理侧栏可见性，将会话操作交给 useChat。输入区在切换会话时保持挂载，
 * 并拥有本地文本和附件草稿。
 */

import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'

import type { RuntimeClient } from '../api/client'
import type { AgentDefinition, RuntimeConfig } from '../config'
import { useChat } from '../composables/useChat'
import { createCheckpointStore } from '../storage/sessionStore'
import AppSidebar from './AppSidebar.vue'
import ChatComposer from './ChatComposer.vue'
import AgentPicker from './AgentPicker.vue'
import ChatContextPanel from './ChatContextPanel.vue'
import MessageTimeline from './MessageTimeline.vue'
import PendingInputList from './PendingInputList.vue'
import ResumePanel from './ResumePanel.vue'
import TodoPanel from './TodoPanel.vue'

const props = defineProps<{
  config: RuntimeConfig
  client: RuntimeClient
  agents: AgentDefinition[]
  runtimeAvailable: boolean
  initialAgentId?: string
}>()

const chat = useChat({
  config: { ...props.config, agents: props.agents },
  client: props.client,
  checkpointStore: createCheckpointStore(() => sessionStorage),
})
const sidebarOpen = ref(false)
const composer = ref<InstanceType<typeof ChatComposer> | null>(null)
const conversationTitle = computed(
  () =>
    chat.sessions.value.find(
      (session) =>
        session.sessionId === chat.selectedSessionId.value &&
        session.agentId === chat.selectedAgent.value.id,
    )?.title ?? '新的对话',
)

/**
 * 隐藏响应式会话侧栏，不改变选中会话。
 */
function closeSidebar(): void {
  sidebarOpen.value = false
}

/**
 * 将历史恢复交给对话控制器前先关闭导航。
 */
async function selectSession(sessionId: string, agentId: string): Promise<void> {
  closeSidebar()
  await chat.selectSession(sessionId, agentId)
}

/**
 * 关闭导航并选择新的本地草稿；实际发送后才持久化。
 */
async function createSession(): Promise<void> {
  closeSidebar()
  await chat.createSession()
  await nextTick()
  composer.value?.focus()
}

function onGlobalKeydown(event: KeyboardEvent): void {
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'n') {
    event.preventDefault()
    void createSession()
  }
}

/**
 * 供宿主页面发送纯文本用户消息，用于快捷提问。
 */
function sendText(text: string): void {
  void chat.send([{ type: 'text', text }])
}

defineExpose({ sendText })

onMounted(() => {
  window.addEventListener('keydown', onGlobalKeydown)
  const initial = props.initialAgentId
  if (
    initial &&
    initial !== chat.selectedAgent.value.id &&
    props.agents.some((agent) => agent.id === initial)
  ) {
    void chat.selectAgent(initial)
  }
})
onBeforeUnmount(() => window.removeEventListener('keydown', onGlobalKeydown))
</script>

<template>
  <div class="app-shell">
    <AppSidebar
      :sessions="chat.sessions.value"
      :selected-session-id="chat.selectedSessionId.value"
      :selected-agent-id="chat.selectedAgent.value.id"
      :loading="chat.catalog.loading.value"
      :has-more="chat.catalog.nextCursor.value !== null"
      :catalog-error="chat.catalog.error.value"
      :rename="chat.catalog.rename"
      :renaming="chat.catalog.renaming.value"
      :removing="chat.catalog.removing.value"
      :open="sidebarOpen"
      :runtime-available="runtimeAvailable"
      @create="createSession"
      @select="selectSession"
      @remove="chat.removeSession"
      @load-more="chat.catalog.load(true)"
      @retry="chat.catalog.load()"
      @close="closeSidebar"
    >
      <template #agent>
        <AgentPicker
          :agents="agents"
          :agent="chat.selectedAgent.value"
          @agent-change="chat.selectAgent"
        />
      </template>
    </AppSidebar>

    <section class="chat-workspace">
      <header class="chat-header">
        <button
          class="menu-button"
          type="button"
          aria-label="打开对话列表"
          @click="sidebarOpen = true"
        >
          <svg viewBox="0 0 20 20" aria-hidden="true">
            <path d="M3 5h14M3 10h14M3 15h14" />
          </svg>
        </button>
        <h1 class="conversation-title" :title="conversationTitle">{{
          conversationTitle
        }}</h1>
      </header>

      <MessageTimeline
        :messages="chat.messages.value"
        :timeline="chat.timeline.value"
        :is-busy="chat.isBusy.value"
        :read-asset="client.readAsset"
      />

      <div class="interaction-dock">
        <TodoPanel
          :key="
            JSON.stringify([chat.selectedAgent.value.id, chat.selectedSessionId.value])
          "
          :todos="chat.todos.items.value"
          :error="chat.todos.error.value"
          :loading="chat.todos.loading.value"
          @retry="chat.todos.refresh"
        />
        <PendingInputList :inputs="chat.pendingInputs.value" />

        <div v-if="chat.storageWarning.value" class="error-banner" role="status">
          <span>{{ chat.storageWarning.value }}</span>
        </div>

        <div v-if="chat.error.value" class="error-banner" role="alert">
          <svg viewBox="0 0 20 20" aria-hidden="true">
            <path d="M10 3 2 17h16L10 3Zm0 5v4m0 2v.1" />
          </svg>
          <span>{{ chat.error.value }}</span>
          <button type="button" @click="chat.reconnect">重新读取</button>
        </div>

        <ResumePanel
          v-if="chat.snapshot.value?.status === 'pending'"
          :expires-at="chat.snapshot.value.result?.suspend_expires_at ?? undefined"
          @resume="chat.resume"
        />

        <ChatComposer
          ref="composer"
          v-model:mode="chat.submissionMode.value"
          :can-steer="chat.isRunning.value"
          :submitting="chat.isSending.value"
          :upload-asset="client.uploadAsset"
          :submit-content="chat.send"
          @cancel="chat.cancel"
        />
      </div>
    </section>

    <ChatContextPanel
      :snapshot="chat.snapshot.value"
      :timeline="chat.timeline.value"
      :agent="chat.selectedAgent.value"
    />
  </div>
</template>
