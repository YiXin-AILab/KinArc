<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import type { AgentStatus, AgentSummary } from '../api/platformClient'
import { defaultPlatformClient } from './client'
import { showToast } from './toast'

const client = defaultPlatformClient()
const router = useRouter()

const agents = ref<AgentSummary[] | null>(null)
const error = ref<string | null>(null)
const deleting = ref<AgentSummary | null>(null)
const deleteBusy = ref(false)

const stats = computed(() => {
  const list = agents.value ?? []
  return {
    total: list.length,
    active: list.filter((agent) => agent.status === 'active').length,
    pending: list.filter((agent) => agent.status === 'pending').length,
    invalid: list.filter((agent) => agent.status === 'invalid').length,
  }
})

function statusMeta(status: AgentStatus): { label: string; cls: string } {
  if (status === 'active') return { label: '生效中', cls: 'platform-b-green' }
  if (status === 'pending') return { label: '待重启生效', cls: 'platform-b-amber' }
  return { label: '配置无效', cls: 'platform-b-red' }
}

async function load(): Promise<void> {
  error.value = null
  try {
    agents.value = await client.listAgents()
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : 'Agent 列表加载失败'
  }
}

async function confirmDelete(): Promise<void> {
  const target = deleting.value
  if (!target) return
  deleteBusy.value = true
  try {
    await client.deleteAgent(target.agent_id)
    showToast(`✓ 已删除 ${target.agent_id} · 重启后生效`)
    deleting.value = null
    await load()
  } catch (caught) {
    showToast(caught instanceof Error ? caught.message : '删除失败', 'error')
  } finally {
    deleteBusy.value = false
  }
}

onMounted(() => void load())
</script>

<template>
  <div class="platform-root">
    <div class="platform-page">
      <div class="platform-page-inner">
        <div class="platform-page-head">
          <div>
            <h1>Agent 管理</h1>
            <p
              >每个 Agent 对应
              <span class="platform-mono"
                >application/&lt;agent&gt;/config/agent.yaml</span
              >，页面保存后重启生效</p
            >
          </div>
          <button
            class="platform-btn platform-btn-pri"
            @click="router.push('/platform/agents/new')"
            >＋ 新建 Agent</button
          >
        </div>

        <div v-if="error" class="platform-state">
          <p role="alert">{{ error }}</p>
          <button class="platform-btn" @click="load">重试</button>
        </div>
        <div v-else-if="!agents" class="platform-state">正在加载 Agent…</div>
        <template v-else>
          <div class="platform-stats">
            <div class="platform-card platform-stat">
              <div class="platform-stat-label">Agent 总数</div>
              <div class="platform-stat-value">{{ stats.total }}</div>
            </div>
            <div class="platform-card platform-stat">
              <div class="platform-stat-label">生效中</div>
              <div class="platform-stat-value" style="color: var(--p-green)">{{
                stats.active
              }}</div>
            </div>
            <div class="platform-card platform-stat">
              <div class="platform-stat-label">待重启生效</div>
              <div class="platform-stat-value" style="color: var(--p-amber)">{{
                stats.pending
              }}</div>
            </div>
            <div class="platform-card platform-stat">
              <div class="platform-stat-label">配置无效</div>
              <div class="platform-stat-value" style="color: var(--p-red)">{{
                stats.invalid
              }}</div>
            </div>
          </div>

          <div class="platform-grid-2">
            <div
              v-for="agent in agents"
              :key="agent.agent_id"
              class="platform-card platform-agent-card"
            >
              <div class="platform-agent-card-head">
                <b>{{ agent.agent_name }}</b>
                <span class="platform-badge" :class="statusMeta(agent.status).cls">
                  <span class="platform-dot" />{{ statusMeta(agent.status).label }}
                </span>
                <span
                  class="platform-badge"
                  :class="
                    agent.output_mode === 'stream'
                      ? 'platform-b-cyan'
                      : 'platform-b-mut'
                  "
                >
                  {{ agent.output_mode }}
                </span>
                <span style="margin-left: auto" class="platform-mono platform-dim">{{
                  agent.agent_id
                }}</span>
              </div>
              <p class="platform-agent-card-desc">{{
                agent.description ?? '（无描述）'
              }}</p>
              <p v-if="agent.error" class="platform-agent-card-error"
                >✕ {{ agent.error }}</p
              >
              <div class="platform-agent-card-tags">
                <span class="platform-badge platform-b-mut">{{
                  agent.model ?? '未配置模型'
                }}</span>
                <span class="platform-badge platform-b-mut"
                  >{{ agent.tool_count }} 工具</span
                >
                <span class="platform-badge platform-b-violet"
                  >{{ agent.skill_count }} Skill</span
                >
              </div>
              <div class="platform-agent-card-ops">
                <button
                  class="platform-btn platform-btn-sm"
                  @click="router.push(`/platform/agents/${agent.agent_id}/edit`)"
                  >编辑配置</button
                >
                <button
                  class="platform-btn platform-btn-sm platform-btn-ghost"
                  @click="router.push(`/platform/agents/new?from=${agent.agent_id}`)"
                  >复制</button
                >
                <button
                  class="platform-btn platform-btn-sm platform-btn-ghost"
                  @click="router.push(`/chat?agent=${agent.agent_id}`)"
                  >测试会话</button
                >
                <button
                  class="platform-btn platform-btn-sm platform-btn-ghost platform-btn-dngr"
                  style="margin-left: auto"
                  @click="deleting = agent"
                  >删除</button
                >
              </div>
            </div>

            <div
              class="platform-card platform-newcard"
              @click="router.push('/platform/agents/new')"
            >
              <div>
                <div class="platform-newcard-plus">＋</div>
                <div style="font-size: 13.5px"
                  >新增业务场景<br /><span class="platform-dim" style="font-size: 12px"
                    >从模板复制 · 或从零创建</span
                  ></div
                >
              </div>
            </div>
          </div>
        </template>
      </div>
    </div>

    <Teleport to="body">
      <div v-if="deleting" class="platform-modal-mask" @click.self="deleting = null">
        <div class="platform-modal" role="dialog" aria-label="删除 Agent">
          <h3>删除 Agent「{{ deleting.agent_name }}」</h3>
          <p>
            将删除 <span class="platform-mono">{{ deleting.agent_id }}</span> 的配置目录
            <template v-if="deleting.status === 'invalid'"
              >（当前配置无效，也可删除）</template
            >
            ，重启后生效。此操作不可撤销。
          </p>
          <div class="platform-modal-actions">
            <button
              class="platform-btn platform-btn-sm"
              :disabled="deleteBusy"
              @click="deleting = null"
              >取消</button
            >
            <button
              class="platform-btn platform-btn-sm platform-btn-dngr"
              :disabled="deleteBusy"
              @click="confirmDelete"
            >
              {{ deleteBusy ? '删除中…' : '确认删除' }}
            </button>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>
