<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { defaultKbClient, type KbClient, type KbSummary } from '../kbClient'
import type { AgentKnowledgeRef, AgentFormState } from '../useAgentForm'

interface KbRefRow {
  name: string
  kb: KbSummary | null
  ref: AgentKnowledgeRef | null
}

const props = withDefaults(
  defineProps<{
    form: AgentFormState
    /** 当前 Agent id：应用级库同名覆盖全局库，仅用于展示归属。 */
    currentAgentId?: string
    client?: KbClient
  }>(),
  { currentAgentId: undefined, client: undefined },
)
const client = props.client ?? defaultKbClient()

const bases = ref<KbSummary[] | null>(null)
const loadError = ref<string | null>(null)
/** 覆盖策略时缓存的 kb.yaml manifest（retrieval 含 ${VAR} 占位符，原样写回）。 */
const manifests = ref<Record<string, Record<string, unknown>>>({})

/** 展示行：平台全部库 + 已选但平台已不存在的引用（保留以免静默丢配置）。 */
const rows = computed<KbRefRow[]>(() => {
  const byName = new Map<string, AgentKnowledgeRef>()
  for (const item of props.form.knowledgeBases) byName.set(item.name, item)
  const known = new Set<string>()
  const list: KbRefRow[] = (bases.value ?? []).map((kb) => {
    known.add(kb.name)
    return { name: kb.name, kb, ref: byName.get(kb.name) ?? null }
  })
  for (const item of props.form.knowledgeBases) {
    if (!known.has(item.name)) list.push({ name: item.name, kb: null, ref: item })
  }
  return list
})

function toggle(row: KbRefRow, checked: boolean): void {
  const index = props.form.knowledgeBases.findIndex((ref) => ref.name === row.name)
  if (checked && index < 0) {
    props.form.knowledgeBases.push({
      name: row.name,
      retrieval: null,
      gate: 'follow',
      gateCustom: null,
    })
  }
  if (!checked && index >= 0) props.form.knowledgeBases.splice(index, 1)
}

function scopeBadge(kb: KbSummary): { label: string; cls: string } {
  if (kb.scope === 'global') return { label: '全局', cls: 'platform-b-green' }
  if (kb.scope === props.currentAgentId) {
    return { label: '本 Agent 应用级', cls: 'platform-b-cyan' }
  }
  return { label: `${kb.scope} 应用级`, cls: 'platform-b-mut' }
}

function strategyOf(ref: AgentKnowledgeRef | null): string {
  const strategy = ref?.retrieval?.strategy
  return typeof strategy === 'string' ? strategy : ''
}

/**
 * 选择覆盖策略：取该库 kb.yaml 的 retrieval 原文（含 embedding 与 ${VAR}
 * 占位符）整体作为覆盖对象，仅替换 strategy/top_k；切回跟随则清空覆盖。
 */
async function applyStrategy(row: KbRefRow, strategy: string): Promise<void> {
  const ref = row.ref
  if (!ref) return
  if (!strategy) {
    ref.retrieval = null
    return
  }
  if (row.kb) {
    const key = `${row.kb.scope}/${row.kb.name}`
    if (!manifests.value[key]) {
      try {
        const manifest = await client.getManifest(row.kb.scope, row.kb.name)
        manifests.value[key] = manifest.config
      } catch {
        // manifest 加载失败时退化为仅写 strategy/top_k，由服务端校验兜底。
      }
    }
    const base = manifests.value[key]?.retrieval
    const retrieval: Record<string, unknown> =
      typeof base === 'object' && base !== null && !Array.isArray(base)
        ? { ...(base as Record<string, unknown>) }
        : {}
    retrieval.strategy = strategy
    if (typeof retrieval.top_k !== 'number') retrieval.top_k = row.kb.top_k ?? 8
    ref.retrieval = retrieval
  } else {
    ref.retrieval = { strategy, top_k: 8 }
  }
}

function onStrategyChange(row: KbRefRow, event: Event): void {
  void applyStrategy(row, (event.target as HTMLSelectElement).value)
}

function setTopK(ref: AgentKnowledgeRef, event: Event): void {
  if (!ref.retrieval) return
  const value = Number((event.target as HTMLInputElement).value)
  ref.retrieval.top_k = Number.isFinite(value) && value >= 1 ? Math.floor(value) : 8
}

function setGate(ref: AgentKnowledgeRef, event: Event): void {
  const value = (event.target as HTMLSelectElement).value
  if (value === 'follow' || value === 'disable') ref.gate = value
}

onMounted(async () => {
  try {
    bases.value = await client.listBases()
  } catch (caught) {
    loadError.value = caught instanceof Error ? caught.message : '知识库列表加载失败'
  }
})
</script>

<template>
  <div class="platform-fsec" id="sec-kb">
    <h3>知识库</h3>
    <p class="platform-mut" style="font-size: 12.5px; margin: -6px 0 12px">
      勾选即写入 agent.yaml 的 knowledge_bases。策略归属三层：库默认（库设置）→ Agent
      使用（本区，跟随或覆盖）→ 测试调试（检索测试页，不影响配置）。
    </p>
    <div v-if="loadError" class="platform-field-error" role="alert">
      ✕ {{ loadError }}
    </div>
    <div v-else-if="!bases" class="platform-mut" style="font-size: 12.5px">
      正在加载知识库…
    </div>
    <div v-else-if="!rows.length" class="platform-dim" style="font-size: 12.5px">
      平台暂无知识库，可先到「知识库」页新建
    </div>
    <div v-for="row in rows" :key="row.name" class="kb-ref-row">
      <input
        type="checkbox"
        :checked="row.ref !== null"
        :aria-label="`引用知识库 ${row.name}`"
        @change="toggle(row, ($event.target as HTMLInputElement).checked)"
      />
      <div style="flex: 1; min-width: 0">
        <b class="platform-mono">{{ row.name }}</b>
        <template v-if="row.kb">
          <span class="platform-badge" :class="scopeBadge(row.kb).cls">{{
            scopeBadge(row.kb).label
          }}</span>
          <span
            v-if="!row.kb.valid"
            class="platform-badge platform-b-red"
            :title="row.kb.error ?? ''"
            >无效</span
          >
          <div class="platform-mut" style="font-size: 12px">
            库默认 {{ row.kb.strategy ?? '—' }} · top_k {{ row.kb.top_k ?? '—' }} · 裁判
            {{ row.kb.has_gate ? '✓' : '—' }}
          </div>
        </template>
        <div v-else class="platform-hint"
          >⚠ 平台已不存在该知识库，保存将原样保留引用</div
        >
      </div>
      <template v-if="row.ref">
        <select
          :value="strategyOf(row.ref)"
          style="width: auto; min-width: 150px"
          :aria-label="`${row.name} 检索策略`"
          @change="onStrategyChange(row, $event)"
        >
          <option value="">跟随库默认（{{ row.kb?.strategy ?? '—' }}）</option>
          <option value="bm25">覆盖： bm25</option>
          <option value="vector">覆盖： vector</option>
          <option value="hybrid">覆盖： hybrid</option>
        </select>
        <input
          v-if="row.ref.retrieval"
          type="number"
          min="1"
          max="100"
          style="width: 76px"
          :value="row.ref.retrieval.top_k"
          :aria-label="`${row.name} top_k`"
          @change="setTopK(row.ref, $event)"
        />
        <select
          :value="row.ref.gate"
          style="width: auto; min-width: 120px"
          :aria-label="`${row.name} 裁判策略`"
          :disabled="row.ref.gate === 'custom'"
          @change="setGate(row.ref, $event)"
        >
          <option value="follow">裁判：跟随库默认</option>
          <option value="disable">裁判：关闭</option>
          <option v-if="row.ref.gate === 'custom'" value="custom">
            裁判：自定义（保留）
          </option>
        </select>
      </template>
    </div>
  </div>
</template>

<style scoped>
.kb-ref-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  border: 1px solid var(--p-border);
  border-radius: 10px;
  margin-bottom: 8px;
}
.kb-ref-row input[type='checkbox'] {
  width: auto;
  accent-color: var(--p-cyan);
}
.kb-ref-row select,
.kb-ref-row input[type='number'] {
  padding: 5px 26px 5px 9px;
  font-size: 12px;
}
.kb-ref-row input[type='number'] {
  padding-right: 9px;
}
</style>
