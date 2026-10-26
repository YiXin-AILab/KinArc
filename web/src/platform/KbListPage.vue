<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import type { AgentSummary } from '../api/platformClient'
import { defaultPlatformClient } from './client'
import { defaultKbClient, type KbSummary } from './kbClient'
import { showToast } from './toast'

const router = useRouter()
const client = defaultKbClient()
const platformClient = defaultPlatformClient()

const bases = ref<KbSummary[] | null>(null)
const agents = ref<AgentSummary[]>([])
const error = ref<string | null>(null)
const creating = ref(false)
const createScope = ref('global')
const createName = ref('')
const createDescription = ref('')
const createBusy = ref(false)

const firstValidBase = computed(() => bases.value?.find((kb) => kb.valid) ?? null)

function workspacePath(kb: KbSummary): string {
  return `/platform/knowledge/${encodeURIComponent(kb.scope)}/${encodeURIComponent(kb.name)}`
}

function scopeLabel(kb: KbSummary): string {
  return kb.scope === 'global' ? '全局' : `${kb.scope} 应用`
}

async function load(): Promise<void> {
  error.value = null
  try {
    const [kbList, agentList] = await Promise.all([
      client.listBases(),
      platformClient.listAgents().catch(() => [] as AgentSummary[]),
    ])
    bases.value = kbList
    agents.value = agentList
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : '知识库列表加载失败'
  }
}

function goTest(): void {
  const target = firstValidBase.value
  if (!target) {
    showToast('暂无可用知识库，请先新建', 'error')
    return
  }
  router.push(`${workspacePath(target)}/test`)
}

function openCreate(): void {
  createScope.value = 'global'
  createName.value = ''
  createDescription.value = ''
  creating.value = true
}

async function confirmCreate(): Promise<void> {
  const name = createName.value.trim()
  if (!name) {
    showToast('知识库名称不能为空', 'error')
    return
  }
  createBusy.value = true
  try {
    const kb = await client.createBase({
      scope: createScope.value,
      name,
      description: createDescription.value.trim(),
    })
    showToast(`✓ 已创建知识库 ${kb.name}`)
    creating.value = false
    router.push(workspacePath(kb))
  } catch (caught) {
    showToast(caught instanceof Error ? caught.message : '创建失败', 'error')
  } finally {
    createBusy.value = false
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
            <h1>知识库资源</h1>
            <p>
              全局库对所有 Agent 可见 · 应用级库同名时覆盖全局 ·
              文档保存即生效（下次检索自动重建索引）
            </p>
          </div>
          <div style="display: flex; gap: 8px">
            <button
              class="platform-btn platform-btn-ghost"
              @click="router.push('/platform/knowledge/generate')"
              >✨ 生成评测集</button
            >
            <button class="platform-btn platform-btn-ghost" @click="goTest"
              >🔍 检索测试</button
            >
            <button class="platform-btn platform-btn-pri" @click="openCreate"
              >＋ 新建知识库</button
            >
          </div>
        </div>

        <div v-if="error" class="platform-state">
          <p role="alert">{{ error }}</p>
          <button class="platform-btn" @click="load">重试</button>
        </div>
        <div v-else-if="!bases" class="platform-state">正在加载知识库…</div>
        <div v-else-if="!bases.length" class="platform-state">
          暂无知识库，点击右上角「新建知识库」开始
        </div>
        <div v-else class="platform-grid-3">
          <div v-for="kb in bases" :key="`${kb.scope}/${kb.name}`" class="kb-card">
            <div class="kb-card-head">
              <b>{{ kb.name }}</b>
              <span
                class="platform-badge"
                :class="kb.scope === 'global' ? 'platform-b-green' : 'platform-b-cyan'"
                >{{ scopeLabel(kb) }}</span
              >
              <span
                v-if="!kb.valid"
                class="platform-badge platform-b-red"
                :title="kb.error ?? ''"
                >无效</span
              >
            </div>
            <p class="platform-mut kb-card-desc">{{
              kb.description || '（无描述）'
            }}</p>
            <div class="kb-card-stats">
              <span>{{ kb.doc_count ?? '—' }} 篇文档</span>
              <span>默认 {{ kb.strategy ?? '—' }}</span>
              <span>裁判 {{ kb.has_gate ? '✓' : '—' }}</span>
              <span>rerank {{ kb.has_rerank ? '✓' : '—' }}</span>
            </div>
            <div class="platform-dim kb-card-refs">
              <template v-if="kb.referenced_by.length">
                被 {{ kb.referenced_by.length }} 个 Agent 引用：{{
                  kb.referenced_by.join('、')
                }}
              </template>
              <template v-else>未被任何 Agent 引用</template>
            </div>
            <div class="kb-card-ops">
              <button
                class="platform-btn platform-btn-sm platform-btn-pri"
                @click="router.push(workspacePath(kb))"
                >管理文档</button
              >
              <button
                class="platform-btn platform-btn-sm platform-btn-ghost"
                @click="router.push(`${workspacePath(kb)}/test`)"
                >检索测试</button
              >
            </div>
          </div>
        </div>
      </div>
    </div>

    <Teleport to="body">
      <div v-if="creating" class="platform-modal-mask" @click.self="creating = false">
        <div class="platform-modal" role="dialog" aria-label="新建知识库">
          <h3>新建知识库</h3>
          <p>创建默认 bm25 检索配置的 kb.yaml 与空文档文件，保存后立即可检索。</p>
          <label>归属范围</label>
          <select v-model="createScope" aria-label="知识库范围">
            <option value="global">全局（knowledge/）</option>
            <option
              v-for="agent in agents"
              :key="agent.agent_id"
              :value="agent.agent_id"
            >
              应用级 · {{ agent.agent_id }}
            </option>
          </select>
          <label style="margin-top: 12px">名称（目录名，创建后不可改）</label>
          <input
            v-model="createName"
            type="text"
            class="platform-mono"
            placeholder="pricing"
            aria-label="知识库名称"
          />
          <label style="margin-top: 12px">描述</label>
          <textarea
            v-model="createDescription"
            rows="2"
            placeholder="价格/工时费/服务时长咨询库"
            aria-label="知识库描述"
          />
          <div class="platform-modal-actions">
            <button
              class="platform-btn platform-btn-sm"
              :disabled="createBusy"
              @click="creating = false"
              >取消</button
            >
            <button
              class="platform-btn platform-btn-sm platform-btn-pri"
              :disabled="createBusy"
              @click="confirmCreate"
            >
              {{ createBusy ? '创建中…' : '创建' }}
            </button>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
.kb-card {
  background: var(--p-panel);
  border: 1px solid var(--p-border);
  border-radius: 15px;
  padding: 18px 20px;
  transition: 0.2s;
}
.kb-card:hover {
  border-color: var(--p-border2);
  transform: translateY(-2px);
}
.kb-card-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}
.kb-card-head b {
  font-size: 15.5px;
}
.kb-card-desc {
  font-size: 12.5px;
  margin: 0 0 12px;
  min-height: 20px;
}
.kb-card-stats {
  display: flex;
  gap: 14px;
  flex-wrap: wrap;
  font-size: 12px;
  color: var(--p-mut);
  margin-bottom: 8px;
}
.kb-card-refs {
  font-size: 12px;
  min-height: 18px;
}
.kb-card-ops {
  display: flex;
  gap: 8px;
  border-top: 1px solid var(--p-border);
  margin-top: 12px;
  padding-top: 12px;
}
</style>
