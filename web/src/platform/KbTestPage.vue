<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import {
  BATCH_TEMPLATE_HEADER,
  defaultKbClient,
  parseBatchCsv,
  type KbBatchItem,
  type KbBatchQuestion,
  type KbBatchSearchResponse,
  type KbDocument,
  type KbQuestionSetSummary,
  type KbRetrievalOverrides,
  type KbSearchChunk,
  type KbSearchResponse,
  type KbSummary,
} from './kbClient'
import { showToast } from './toast'

type TestTab = 'single' | 'batch'
type BatchSource = 'set' | 'file' | 'text'

const route = useRoute()
const router = useRouter()
const client = defaultKbClient()

const scope = String(route.params.scope)
const name = String(route.params.name)

const tab = ref<TestTab>(route.query.tab === 'batch' ? 'batch' : 'single')
const bases = ref<KbSummary[]>([])
const docs = ref<KbDocument[]>([])
const questionSets = ref<KbQuestionSetSummary[]>([])
const loadError = ref<string | null>(null)

const strategy = ref<'' | 'bm25' | 'vector' | 'hybrid'>('')
const rerankEnabled = ref(false)
const gateEnabled = ref(true)
const scopeChecked = ref<Set<string>>(new Set())

const singleQuery = ref('')
const singleRunning = ref(false)
const singleResult = ref<KbSearchResponse | null>(null)
const singleError = ref<string | null>(null)

const batchSource = ref<BatchSource>('set')
const selectedSet = ref(typeof route.query.set === 'string' ? route.query.set : '')
/** 与批测请求同序的参考答案（响应不携带原文，diff 左栏展示用）。 */
const referenceAnswers = ref<string[]>([])
const fileName = ref('')
const fileQuestions = ref<KbBatchQuestion[]>([])
const pastedText = ref('')
const batchRunning = ref(false)
const batchResult = ref<KbBatchSearchResponse | null>(null)
const batchError = ref<string | null>(null)
const expandedChunks = ref<Set<number>>(new Set())

const currentKb = computed(
  () => bases.value.find((kb) => kb.scope === scope && kb.name === name) ?? null,
)

const scopeAll = computed(
  () => docs.value.length > 0 && scopeChecked.value.size === docs.value.length,
)

const singleScopeSummary = computed(() => {
  if (!docs.value.length) return '范围：全部文档'
  return scopeAll.value
    ? `范围：全部文档（${docs.value.length} 篇）`
    : `范围：已选 ${scopeChecked.value.size} 篇`
})

const batchReady = computed(() => {
  if (batchSource.value === 'set') return Boolean(selectedSet.value)
  if (batchSource.value === 'file') return fileQuestions.value.length > 0
  return pastedText.value.trim().length > 0
})

function gateTag(chunk: KbSearchChunk): { label: string; cls: string } | null {
  if (!chunk.gate_relevance) return null
  if (chunk.gate_error) return { label: '裁判失败', cls: 'platform-b-amber' }
  if (chunk.gate_relevance === 'irrelevant') {
    return { label: 'irrelevant ✗ 被裁判剔除', cls: 'platform-b-mut' }
  }
  return { label: `${chunk.gate_relevance} ✓`, cls: 'platform-b-green' }
}

function buildOverrides(): KbRetrievalOverrides {
  const overrides: KbRetrievalOverrides = {}
  if (strategy.value) overrides.strategy = strategy.value
  const kb = currentKb.value
  if (kb?.has_rerank && !rerankEnabled.value) overrides.rerank = { enabled: false }
  if (kb?.has_gate && !gateEnabled.value) overrides.gate = { enabled: false }
  return overrides
}

function switchKb(event: Event): void {
  const value = (event.target as HTMLSelectElement).value
  const target = bases.value.find((kb) => `${kb.scope}/${kb.name}` === value)
  if (!target) return
  router.push({
    path: `/platform/knowledge/${target.scope}/${target.name}/test`,
    query: { tab: tab.value },
  })
}

function toggleScopeDoc(docId: string, checked: boolean): void {
  const next = new Set(scopeChecked.value)
  if (checked) next.add(docId)
  else next.delete(docId)
  scopeChecked.value = next
}

function selectAllScope(): void {
  scopeChecked.value = new Set(docs.value.map((doc) => doc.doc_id))
}

async function runSingle(): Promise<void> {
  const query = singleQuery.value.trim()
  if (!query || singleRunning.value) return
  singleRunning.value = true
  singleError.value = null
  singleResult.value = null
  try {
    const request = {
      query,
      ...buildOverrides(),
      ...(scopeAll.value || !docs.value.length
        ? {}
        : { doc_scope: [...scopeChecked.value] }),
    }
    singleResult.value = await client.search(scope, name, request)
  } catch (caught) {
    singleError.value = caught instanceof Error ? caught.message : '单测执行失败'
  } finally {
    singleRunning.value = false
  }
}

async function onFileChange(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  const text = await file.text()
  fileQuestions.value = parseBatchCsv(text)
  fileName.value = file.name
  if (!fileQuestions.value.length) {
    showToast('文件中没有解析到问题（第一列必填）', 'error')
  } else {
    showToast(`✓ 已解析 ${fileQuestions.value.length} 个问题`)
  }
}

function downloadTemplate(): void {
  const example =
    'S4 A 保多少钱,pricing-007|pricing-021,"A 保参考 1400-1700 元，约 1.5-2 小时"\n'
  const blob = new Blob([`${BATCH_TEMPLATE_HEADER}\n${example}`], {
    type: 'text/csv;charset=utf-8',
  })
  const link = document.createElement('a')
  link.href = URL.createObjectURL(blob)
  link.download = 'batch_test_template.csv'
  link.click()
  URL.revokeObjectURL(link.href)
}

async function runBatch(): Promise<void> {
  if (!batchReady.value || batchRunning.value) return
  const overrides = buildOverrides()
  let request
  let answers: string[] = []
  if (batchSource.value === 'set') {
    request = { question_set: selectedSet.value, ...overrides }
    answers = await loadSetAnswers(selectedSet.value)
  } else if (batchSource.value === 'file') {
    request = { questions: fileQuestions.value, ...overrides }
    answers = fileQuestions.value.map((item) => item.reference_answer ?? '')
  } else {
    const questions = pastedText.value
      .split('\n')
      .map((line) => line.trim())
      .filter(Boolean)
      .map((question) => ({ question }))
    request = { questions, ...overrides }
  }
  batchRunning.value = true
  batchError.value = null
  batchResult.value = null
  referenceAnswers.value = []
  expandedChunks.value = new Set()
  try {
    const response = await client.batchSearch(scope, name, request)
    batchResult.value = response
    referenceAnswers.value = answers
  } catch (caught) {
    batchError.value = caught instanceof Error ? caught.message : '批测执行失败'
  } finally {
    batchRunning.value = false
  }
}

/** 读取问题集条目（status != discarded，与后端批测取题规则一致）的参考答案。 */
async function loadSetAnswers(setName: string): Promise<string[]> {
  try {
    const set = await client.getQuestionSet(scope, name, setName)
    return set.items
      .filter((item) => item.status !== 'discarded')
      .map((item) => item.reference_answer ?? '')
  } catch {
    return []
  }
}

function formatRate(rate: number): string {
  return `${(rate * 100).toFixed(1).replace(/\.0$/, '')}%`
}

function itemHeadTag(item: KbBatchItem): { label: string; cls: string } {
  if (item.error) return { label: '执行失败', cls: 'platform-b-red' }
  if (item.doc_metrics) {
    const metrics = item.doc_metrics
    const expected = metrics.expected_doc_ids.join('|')
    if (metrics.set_recall) {
      return {
        label: `期望 ${expected} · Top1 ${metrics.top1_hit ? '✓' : '·'} 全命中 ✓`,
        cls: 'platform-b-green',
      }
    }
    const missing = metrics.missing.map((entry) => entry.doc_id).join('|')
    return { label: `期望 ${expected} · 缺 ${missing} ✗`, cls: 'platform-b-amber' }
  }
  if (item.answer_metrics) {
    const coverage = item.answer_metrics.coverage
    return {
      label:
        coverage === null
          ? '参考答案文本 · 裁判失败'
          : `参考答案文本 · 内容覆盖分 ${coverage.toFixed(2)}`,
      cls: 'platform-b-cyan',
    }
  }
  return { label: '无期望答案', cls: 'platform-b-mut' }
}

function bestChunk(item: KbBatchItem): KbSearchChunk | null {
  return item.chunks.find((chunk) => !chunk.filtered) ?? item.chunks[0] ?? null
}

function toggleChunks(index: number): void {
  const next = new Set(expandedChunks.value)
  if (next.has(index)) next.delete(index)
  else next.add(index)
  expandedChunks.value = next
}

async function load(): Promise<void> {
  loadError.value = null
  try {
    const [kbList, docList, sets] = await Promise.all([
      client.listBases(),
      client.listDocs(scope, name),
      client.listQuestionSets(scope, name).catch(() => []),
    ])
    bases.value = kbList
    docs.value = docList
    scopeChecked.value = new Set(docList.map((doc) => doc.doc_id))
    questionSets.value = sets.filter((set) => set.valid)
    const kb = currentKb.value
    rerankEnabled.value = kb?.has_rerank ?? false
    gateEnabled.value = kb?.has_gate ?? true
    if (
      !selectedSet.value ||
      !questionSets.value.some((set) => set.name === selectedSet.value)
    ) {
      selectedSet.value = questionSets.value.find((set) => set.total > 0)?.name ?? ''
    }
  } catch (caught) {
    loadError.value = caught instanceof Error ? caught.message : '测试页加载失败'
  }
}

watch(tab, (value) => {
  router.replace({ query: { ...route.query, tab: value } })
})

onMounted(() => void load())
</script>

<template>
  <div class="platform-root">
    <div class="kb-crumb">
      <RouterLink to="/platform/knowledge" class="kb-crumb-link">← 知识库</RouterLink>
      <RouterLink :to="`/platform/knowledge/${scope}/${name}`" class="kb-crumb-link">
        {{ name }}
      </RouterLink>
      <b>检索测试</b>
      <span style="margin-left: auto"></span>
      <div class="platform-rtabs" style="border: none; padding: 0">
        <div
          class="platform-rtab"
          :class="{ on: tab === 'single' }"
          data-testid="tab-single"
          @click="tab = 'single'"
          >单测</div
        >
        <div
          class="platform-rtab"
          :class="{ on: tab === 'batch' }"
          data-testid="tab-batch"
          @click="tab = 'batch'"
          >批测</div
        >
      </div>
    </div>

    <div v-if="loadError" class="platform-state">
      <p role="alert">{{ loadError }}</p>
      <button class="platform-btn" @click="load">重试</button>
    </div>
    <div v-else class="kb-test-layout">
      <aside class="kb-test-config">
        <div class="platform-field">
          <label>目标库</label>
          <select :value="`${scope}/${name}`" aria-label="目标库" @change="switchKb">
            <option
              v-for="kb in bases"
              :key="`${kb.scope}/${kb.name}`"
              :value="`${kb.scope}/${kb.name}`"
            >
              {{ kb.name }}（{{ kb.scope === 'global' ? '全局' : kb.scope }} · 默认
              {{ kb.strategy ?? '—' }}）
            </option>
          </select>
        </div>
        <div class="platform-field">
          <label>检索策略（测试用，不改配置）</label>
          <select v-model="strategy" aria-label="检索策略">
            <option value="">跟随库默认（{{ currentKb?.strategy ?? '—' }}）</option>
            <option value="bm25">bm25</option>
            <option value="vector">vector</option>
            <option value="hybrid">hybrid</option>
          </select>
        </div>
        <div class="platform-field" style="display: flex; gap: 16px">
          <label class="kb-check" :class="{ 'kb-check-off': !currentKb?.has_rerank }">
            <input
              v-model="rerankEnabled"
              type="checkbox"
              :disabled="!currentKb?.has_rerank"
            />
            rerank{{ currentKb?.has_rerank ? '' : '（库未配置）' }}
          </label>
          <label class="kb-check" :class="{ 'kb-check-off': !currentKb?.has_gate }">
            <input
              v-model="gateEnabled"
              type="checkbox"
              :disabled="!currentKb?.has_gate"
            />
            相关性裁判{{ currentKb?.has_gate ? '' : '（库未配置）' }}
          </label>
        </div>
        <div class="platform-field">
          <label>测试范围</label>
          <div style="display: flex; gap: 6px; align-items: center; margin-bottom: 6px">
            <span class="platform-badge platform-b-cyan">{{
              scopeAll
                ? `全部文档（${docs.length} 篇）✓`
                : `已选 ${scopeChecked.size} / ${docs.length} 篇`
            }}</span>
            <button
              v-if="!scopeAll"
              class="platform-btn platform-btn-ghost platform-btn-sm"
              @click="selectAllScope"
              >全选</button
            >
          </div>
          <div class="kb-scope-list">
            <label v-for="doc in docs" :key="doc.doc_id" class="kb-check">
              <input
                type="checkbox"
                :checked="scopeChecked.has(doc.doc_id)"
                @change="
                  toggleScopeDoc(
                    doc.doc_id,
                    ($event.target as HTMLInputElement).checked,
                  )
                "
              />
              <span class="platform-mono">{{ doc.doc_id }}</span> {{ doc.title }}
            </label>
            <span v-if="!docs.length" class="platform-dim" style="font-size: 12px"
              >库内暂无文档</span
            >
          </div>
          <div class="platform-dim" style="font-size: 11px; margin-top: 4px">
            可多选圈定范围（仅单测生效），用于只改了某几篇后快速验证
          </div>
        </div>

        <template v-if="tab === 'single'">
          <div class="platform-field">
            <label>测试问题</label>
            <textarea
              v-model="singleQuery"
              rows="3"
              placeholder="S4 A 保多少钱，四驱有什么要特别注意的"
              aria-label="测试问题"
            ></textarea>
          </div>
          <button
            class="platform-btn platform-btn-pri"
            style="width: 100%"
            :disabled="singleRunning || !singleQuery.trim()"
            data-testid="run-single"
            @click="runSingle"
          >
            {{ singleRunning ? '检索中…' : '运行单测' }}
          </button>
        </template>

        <template v-else>
          <div class="platform-field">
            <label>问题来源（三选一）</label>
            <div class="kb-source-box">
              <label class="kb-check" style="margin-bottom: 6px">
                <input v-model="batchSource" type="radio" value="set" />
                <b>从问题集选择</b>（推荐）
              </label>
              <select
                v-model="selectedSet"
                :disabled="batchSource !== 'set'"
                aria-label="选择问题集"
                style="margin-bottom: 10px"
              >
                <option value="" disabled>选择问题集…</option>
                <option v-for="set in questionSets" :key="set.name" :value="set.name">
                  {{ set.name }}（{{ set.total }} 条 · 已采纳 {{ set.accepted }}）
                </option>
              </select>
              <label class="kb-check" style="margin-bottom: 6px">
                <input v-model="batchSource" type="radio" value="file" />
                <b>上传文件</b>
              </label>
              <div class="kb-upload">
                <template v-if="fileName">
                  📄 {{ fileName }}（{{ fileQuestions.length }} 个问题）
                </template>
                <template v-else>未选择文件（CSV，问题列必填）</template>
                <div style="margin-top: 6px; display: flex; gap: 6px">
                  <label class="platform-btn platform-btn-ghost platform-btn-sm">
                    {{ fileName ? '重新上传' : '选择文件' }}
                    <input
                      type="file"
                      accept=".csv,.txt"
                      style="display: none"
                      :disabled="batchSource !== 'file'"
                      data-testid="batch-file"
                      @change="onFileChange"
                    />
                  </label>
                  <button
                    class="platform-btn platform-btn-ghost platform-btn-sm"
                    @click="downloadTemplate"
                    >下载模板</button
                  >
                </div>
              </div>
              <label class="kb-check" style="margin: 10px 0 6px">
                <input v-model="batchSource" type="radio" value="text" />
                <b>粘贴文本</b>（每行一个问题）
              </label>
              <textarea
                v-model="pastedText"
                rows="2"
                :disabled="batchSource !== 'text'"
                placeholder="S4 A 保多少钱&#10;工时费怎么算"
                aria-label="粘贴问题"
              ></textarea>
            </div>
            <div class="platform-dim" style="font-size: 11px; margin-top: 6px">
              生成评测集审核采纳后自动存为问题集，此处直接选用，无需导出再上传。
              答案列规则：期望 doc_id（| 分隔）→ 命中率；参考答案文本 → 覆盖分 + diff
            </div>
          </div>
          <button
            class="platform-btn platform-btn-pri"
            style="width: 100%"
            :disabled="batchRunning || !batchReady"
            data-testid="run-batch"
            @click="runBatch"
          >
            {{ batchRunning ? '批测执行中…' : '运行批测' }}
          </button>
          <button
            class="platform-btn platform-btn-ghost"
            style="width: 100%; margin-top: 8px"
            @click="router.push('/platform/knowledge/generate')"
            >✨ 根据文档生成测试问题</button
          >
        </template>

        <div class="kb-note">
          测试页所有策略选择都是<b>调试工具</b>，不修改任何配置。库默认在「库设置」、Agent
          策略在 Agent 配置页。
        </div>
      </aside>

      <main class="kb-test-results">
        <template v-if="tab === 'single'">
          <div v-if="singleError" class="platform-field-error" role="alert">
            ✕ {{ singleError }}
          </div>
          <template v-if="singleResult">
            <div class="kb-result-meta">
              <span class="platform-mut">
                {{ strategy || currentKb?.strategy || '—' }} · 裁判{{
                  gateEnabled && currentKb?.has_gate ? '开启' : '关闭'
                }}
                · {{ singleScopeSummary }}
              </span>
              <span
                v-if="singleResult.no_relevant_docs"
                class="platform-badge platform-b-amber"
                >裁判后无相关文档</span
              >
            </div>
            <div
              v-for="chunk in singleResult.chunks"
              :key="chunk.doc_id"
              class="platform-card kb-chunk"
              :class="{ 'kb-chunk-filtered': chunk.filtered }"
            >
              <b class="platform-mono">{{ chunk.doc_id }}</b>
              <span class="platform-badge platform-b-cyan">{{
                chunk.score.toFixed(2)
              }}</span>
              <span
                v-if="gateTag(chunk)"
                class="platform-badge"
                :class="gateTag(chunk)!.cls"
              >
                {{ gateTag(chunk)!.label }}
              </span>
              <div style="font-size: 13px; margin-top: 6px">{{ chunk.content }}</div>
              <div
                v-if="chunk.filtered && chunk.gate_reason"
                class="platform-mut"
                style="font-size: 12px; margin-top: 4px"
              >
                裁判理由：{{ chunk.gate_reason }}
              </div>
            </div>
            <p class="platform-dim" style="font-size: 12px; margin-top: 8px">
              结果即召回的文档片段原文，按分数排序，含裁判判定与理由，供人工查看。
            </p>
          </template>
          <div v-else-if="!singleError" class="platform-state">
            输入问题并运行单测，查看召回片段与裁判判定
          </div>
        </template>

        <template v-else>
          <div v-if="batchError" class="platform-field-error" role="alert">
            ✕ {{ batchError }}
          </div>
          <template v-if="batchResult">
            <div class="platform-card kb-metric-bar" data-testid="batch-metric-bar">
              <b>指标汇总</b>
              <span v-if="batchResult.summary.doc_questions.count">
                期望 doc_id（{{ batchResult.summary.doc_questions.count }} 题）：Top1
                命中
                <b class="kb-hit-yes">{{
                  formatRate(batchResult.summary.doc_questions.top1_rate)
                }}</b>
                · Top3
                <b class="kb-hit-yes">{{
                  formatRate(batchResult.summary.doc_questions.top3_rate)
                }}</b>
                · 裁判误杀
                <b
                  :class="
                    batchResult.summary.doc_questions.gate_false_kill_count
                      ? 'kb-hit-no'
                      : 'kb-hit-yes'
                  "
                  >{{ batchResult.summary.doc_questions.gate_false_kill_count }}</b
                >
              </span>
              <span v-if="batchResult.summary.answer_questions.count">
                参考答案文本（{{ batchResult.summary.answer_questions.count }}
                题）：内容覆盖均分
                <b class="kb-hit-yes">{{
                  batchResult.summary.answer_questions.average_coverage === null
                    ? '—'
                    : batchResult.summary.answer_questions.average_coverage.toFixed(2)
                }}</b>
                <span class="platform-dim" style="font-size: 11px"
                  >（judge 对照答案评分）</span
                >
              </span>
              <span
                v-if="
                  !batchResult.summary.doc_questions.count &&
                  !batchResult.summary.answer_questions.count
                "
                class="platform-mut"
                >本批题目未携带期望答案，只展示召回结果</span
              >
            </div>

            <div
              v-for="(item, index) in batchResult.results"
              :key="index"
              class="platform-card kb-chunk"
            >
              <div class="kb-qhead">
                <b>Q{{ index + 1 }}. {{ item.question }}</b>
                <span class="platform-badge" :class="itemHeadTag(item).cls">{{
                  itemHeadTag(item).label
                }}</span>
              </div>
              <div v-if="item.error" class="platform-field-error" role="alert">
                ✕ {{ item.error }}
              </div>
              <template v-else>
                <div class="kb-qbody">
                  <div
                    v-for="chunk in item.chunks.slice(
                      0,
                      expandedChunks.has(index) ? undefined : 3,
                    )"
                    :key="chunk.doc_id"
                    style="font-size: 13px; margin-top: 4px"
                    :style="chunk.filtered ? 'opacity:.55' : ''"
                  >
                    <b class="platform-mono">{{ chunk.doc_id }}</b>
                    <span class="platform-badge platform-b-cyan">{{
                      chunk.score.toFixed(2)
                    }}</span>
                    <span
                      v-if="gateTag(chunk)"
                      class="platform-badge"
                      :class="gateTag(chunk)!.cls"
                      >{{ gateTag(chunk)!.label }}</span
                    >
                    — {{ chunk.content.slice(0, 80)
                    }}{{ chunk.content.length > 80 ? '…' : '' }}
                  </div>
                  <button
                    v-if="item.chunks.length > 3"
                    class="platform-btn platform-btn-ghost platform-btn-sm"
                    style="margin-top: 4px"
                    @click="toggleChunks(index)"
                  >
                    {{
                      expandedChunks.has(index)
                        ? '收起'
                        : `展开全部 ${item.chunks.length} 个片段`
                    }}
                  </button>
                  <div
                    v-for="missing in item.doc_metrics?.missing ?? []"
                    :key="missing.doc_id"
                    class="platform-mut"
                    style="font-size: 12px; margin-top: 4px"
                  >
                    诊断：<span class="platform-mono">{{ missing.doc_id }}</span>
                    <template v-if="missing.gate_filtered"
                      >检索命中（候选第 {{ missing.rank }} 位）但被裁判剔除 →
                      可放宽裁判阈值或关闭裁判对比</template
                    >
                    <template v-else-if="missing.rank !== null"
                      >全库第 {{ missing.rank }} 位，未进最终结果 → 可调大
                      top_k</template
                    >
                    <template v-else>不在库中或未进入全库排名</template>
                  </div>
                  <div
                    v-if="item.doc_metrics?.gate_false_kills.length"
                    class="platform-field-error"
                    style="margin-top: 6px"
                  >
                    ⚠ 裁判误杀期望文档：{{
                      item.doc_metrics.gate_false_kills.join('、')
                    }}
                  </div>
                </div>

                <div v-if="item.answer_metrics" class="kb-diff">
                  <div class="kb-diff-grid">
                    <div>
                      <div class="kb-diff-head">参考答案</div>
                      <div class="kb-diff-text">
                        {{ referenceAnswers[index] || '（无参考答案原文）' }}
                      </div>
                    </div>
                    <div>
                      <div class="kb-diff-head">
                        最佳召回片段（{{ bestChunk(item)?.doc_id ?? '—' }} ·
                        {{ bestChunk(item)?.score.toFixed(2) ?? '—' }}）
                      </div>
                      <div class="kb-diff-text">{{
                        bestChunk(item)?.content ?? '（无召回）'
                      }}</div>
                    </div>
                  </div>
                  <div style="margin-top: 8px; font-size: 12px">
                    <span
                      v-for="token in item.answer_metrics.diff.matched"
                      :key="`m-${token}`"
                      class="kb-diff-token kb-diff-same"
                      >{{ token }}</span
                    >
                    <span
                      v-for="token in item.answer_metrics.diff.missing"
                      :key="`x-${token}`"
                      class="kb-diff-token kb-diff-miss"
                      >{{ token }}</span
                    >
                    <span
                      v-for="token in item.answer_metrics.diff.extra"
                      :key="`e-${token}`"
                      class="kb-diff-token kb-diff-extra"
                      >{{ token }}</span
                    >
                  </div>
                  <div class="platform-mut" style="font-size: 12px; margin-top: 6px">
                    <span class="kb-diff-token kb-diff-same">一致</span>
                    <span class="kb-diff-token kb-diff-miss"
                      >答案有但片段未召回（缺料）</span
                    >
                    <span class="kb-diff-token kb-diff-extra">片段多出（供参考）</span>
                    <template v-if="item.answer_metrics.rationale">
                      · judge 理由：{{ item.answer_metrics.rationale }}
                    </template>
                    <template v-if="item.answer_metrics.error">
                      ·
                      <span style="color: var(--p-red)">{{
                        item.answer_metrics.error
                      }}</span>
                    </template>
                  </div>
                  <div class="platform-dim" style="font-size: 11px; margin-top: 4px">
                    {{ item.answer_metrics.diff_note }}
                  </div>
                </div>
              </template>
            </div>

            <p class="platform-dim" style="margin-top: 10px; font-size: 12px">
              doc_id 答案 → Top1/Top3 命中率；参考答案文本 → judge
              对照答案打「内容覆盖分」并生成词级 diff（与评测同一 judge
              引擎）。换策略重跑即可对比两批指标。
            </p>
          </template>
          <div v-else-if="!batchError" class="platform-state">
            选择问题来源并运行批测，查看命中率与覆盖分指标
          </div>
        </template>
      </main>
    </div>
  </div>
</template>

<style scoped>
.kb-crumb {
  flex: none;
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 24px;
  border-bottom: 1px solid var(--p-border);
  background: rgba(6, 9, 18, 0.6);
  font-size: 13px;
}
.kb-crumb-link {
  color: var(--p-cyan);
  text-decoration: none;
}
.kb-test-layout {
  flex: 1;
  min-height: 0;
  display: flex;
}
.kb-test-config {
  width: 320px;
  flex-shrink: 0;
  border-right: 1px solid var(--p-border);
  padding: 16px;
  overflow-y: auto;
  background: rgba(6, 9, 18, 0.35);
}
.kb-check {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--p-mut);
  margin-bottom: 4px;
}
.kb-check input {
  width: auto;
  accent-color: var(--p-cyan);
}
.kb-check-off {
  opacity: 0.55;
}
.kb-scope-list {
  background: var(--p-panel);
  border: 1px solid var(--p-border);
  border-radius: 10px;
  padding: 8px 12px;
  font-size: 12px;
  max-height: 150px;
  overflow-y: auto;
}
.kb-source-box {
  background: var(--p-panel);
  border: 1px solid var(--p-border);
  border-radius: 10px;
  padding: 10px 12px;
  font-size: 12px;
}
.kb-upload {
  border: 1px dashed var(--p-border);
  border-radius: 8px;
  padding: 8px;
  text-align: center;
}
.kb-note {
  background: rgba(251, 191, 36, 0.08);
  border: 1px solid rgba(251, 191, 36, 0.35);
  border-radius: 8px;
  padding: 8px 12px;
  font-size: 12px;
  color: var(--p-amber);
  margin-top: 16px;
}
.kb-test-results {
  flex: 1;
  min-width: 0;
  overflow-y: auto;
  padding: 20px 28px;
}
.kb-result-meta {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-bottom: 12px;
  font-size: 12px;
}
.kb-chunk {
  padding: 12px 16px;
  margin-top: 10px;
}
.kb-chunk-filtered {
  opacity: 0.55;
}
.kb-metric-bar {
  padding: 12px 16px;
  display: flex;
  gap: 20px;
  align-items: center;
  position: sticky;
  top: -20px;
  z-index: 5;
  font-size: 13px;
  flex-wrap: wrap;
  background: #0b1020;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.3);
}
.kb-hit-yes {
  color: var(--p-green);
}
.kb-hit-no {
  color: var(--p-red);
}
.kb-qhead {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 10px;
}
.kb-qbody {
  margin-top: 8px;
  border-left: 2px solid var(--p-border);
  padding-left: 12px;
}
.kb-diff {
  margin-top: 10px;
}
.kb-diff-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}
.kb-diff-head {
  font-size: 12px;
  color: var(--p-mut);
  margin-bottom: 4px;
}
.kb-diff-text {
  font-size: 13px;
  background: rgba(4, 6, 13, 0.6);
  border: 1px solid var(--p-border);
  border-radius: 8px;
  padding: 10px 12px;
  line-height: 1.9;
  white-space: pre-wrap;
  word-break: break-word;
}
.kb-diff-token {
  display: inline-block;
  border-radius: 4px;
  padding: 1px 5px;
  margin: 0 3px 3px 0;
  font-size: 12px;
}
.kb-diff-same {
  background: rgba(52, 211, 153, 0.22);
}
.kb-diff-miss {
  background: rgba(248, 113, 113, 0.25);
}
.kb-diff-extra {
  background: rgba(251, 191, 36, 0.2);
}
</style>
