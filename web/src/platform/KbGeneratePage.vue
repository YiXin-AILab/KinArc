<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import {
  defaultKbClient,
  parseKbSource,
  type KbGenJob,
  type KbQuestionItem,
  type KbQuestionItemPatch,
  type KbSummary,
} from './kbClient'
import { showToast } from './toast'

interface ReviewRow {
  scope: string
  kbName: string
  setName: string
  index: number
  item: KbQuestionItem
}

const CASE_TYPE_META: Record<string, { label: string; cls: string }> = {
  single_doc: { label: '单文档问答', cls: 'platform-b-cyan' },
  multi_doc: { label: '多文档组合', cls: 'platform-b-amber' },
  boundary: { label: '边界混淆', cls: 'platform-b-amber' },
  out_of_scope: { label: '库外拒答', cls: 'platform-b-mut' },
}

const POLL_INTERVAL_MS = 2000

const route = useRoute()
const router = useRouter()
const client = defaultKbClient()

const bases = ref<KbSummary[]>([])
const selectedSources = ref<Set<string>>(new Set())
const guidance = ref('')
const count = ref(10)
const job = ref<KbGenJob | null>(null)
const generating = ref(false)
const generateError = ref<string | null>(null)

const rows = ref<ReviewRow[]>([])
const queueNotice = ref<string | null>(null)
const editingKey = ref<string | null>(null)
const editQuestion = ref('')
const editDocIds = ref('')
const editAnswer = ref('')
const evalModal = ref(false)
const evalTaskId = ref('')
const evalDataset = ref('')
const evalBusy = ref(false)

let pollTimer: ReturnType<typeof setTimeout> | undefined
const pollController = new AbortController()

const summaryCounts = computed(() => {
  const counts = { total: rows.value.length, accepted: 0, discarded: 0, pending: 0 }
  for (const row of rows.value) counts[row.item.status] += 1
  return counts
})

const jobRunning = computed(
  () => job.value?.status === 'pending' || job.value?.status === 'running',
)

function sourceOf(kb: KbSummary): string {
  return kb.scope === 'global' ? kb.name : `${kb.scope}/${kb.name}`
}

function toggleSource(kb: KbSummary, checked: boolean): void {
  const next = new Set(selectedSources.value)
  const source = sourceOf(kb)
  if (checked) next.add(source)
  else next.delete(source)
  selectedSources.value = next
}

function rowKey(row: ReviewRow): string {
  return `${row.scope}/${row.kbName}/${row.setName}#${row.index}`
}

function expectedLine(item: KbQuestionItem): string {
  if (item.case_type === 'out_of_scope') return '期望：must_refuse'
  const parts = item.expected_doc_ids.length
    ? [`期望 doc_id：${item.expected_doc_ids.join(' | ')}`]
    : []
  if (item.reference_answer) parts.push('含参考答案')
  if (item.forbidden.length) parts.push(`forbidden：${item.forbidden.join('、')}`)
  return parts.join(' · ') || '（无期望标注）'
}

async function runGenerate(): Promise<void> {
  if (!selectedSources.value.size || generating.value) return
  generating.value = true
  generateError.value = null
  job.value = null
  try {
    const started = await client.generate({
      sources: [...selectedSources.value],
      guidance: guidance.value.trim(),
      count: count.value,
    })
    pollJob(started.job_id)
  } catch (caught) {
    generating.value = false
    generateError.value = caught instanceof Error ? caught.message : '生成启动失败'
  }
}

async function pollJob(jobId: string): Promise<void> {
  try {
    const snapshot = await client.getGenJob(jobId, pollController.signal)
    job.value = snapshot
    if (snapshot.status === 'pending' || snapshot.status === 'running') {
      pollTimer = setTimeout(() => void pollJob(jobId), POLL_INTERVAL_MS)
      return
    }
    generating.value = false
    if (snapshot.status === 'succeeded') {
      await loadJobResults(snapshot)
      showToast('✓ 生成完成，候选已进入审核队列')
    } else {
      generateError.value = snapshot.error ?? '生成失败'
    }
  } catch (caught) {
    if (pollController.signal.aborted) return
    generating.value = false
    generateError.value = caught instanceof Error ? caught.message : 'Job 状态查询失败'
  }
}

async function loadJobResults(snapshot: KbGenJob): Promise<void> {
  const merged: ReviewRow[] = []
  for (const result of snapshot.results) {
    if (!result.question_set) continue
    const { scope, name } = parseKbSource(result.source)
    merged.push(...(await loadSetRows(scope, name, result.question_set)))
  }
  rows.value = merged
  queueNotice.value = merged.length
    ? null
    : '生成完成但没有可用候选（可能被去重/校验全部丢弃）'
}

async function loadSetRows(
  scope: string,
  kbName: string,
  setName: string,
): Promise<ReviewRow[]> {
  try {
    const set = await client.getQuestionSet(scope, kbName, setName)
    return set.items.map((item, index) => ({ scope, kbName, setName, index, item }))
  } catch (caught) {
    showToast(
      caught instanceof Error ? caught.message : `问题集 ${setName} 加载失败`,
      'error',
    )
    return []
  }
}

async function patchRow(row: ReviewRow, patch: KbQuestionItemPatch): Promise<void> {
  try {
    const updated = await client.patchQuestionItem(
      row.scope,
      row.kbName,
      row.setName,
      row.index,
      patch,
    )
    row.item = updated
  } catch (caught) {
    showToast(caught instanceof Error ? caught.message : '审核操作失败', 'error')
  }
}

function startEdit(row: ReviewRow): void {
  editingKey.value = rowKey(row)
  editQuestion.value = row.item.question
  editDocIds.value = row.item.expected_doc_ids.join(' | ')
  editAnswer.value = row.item.reference_answer ?? ''
}

async function saveEdit(row: ReviewRow): Promise<void> {
  const question = editQuestion.value.trim()
  if (!question) {
    showToast('问题文本不能为空', 'error')
    return
  }
  const patch: KbQuestionItemPatch = { question }
  patch.expected_doc_ids = editDocIds.value
    .split('|')
    .map((part) => part.trim())
    .filter(Boolean)
  const answer = editAnswer.value.trim()
  if (answer) patch.reference_answer = answer
  await patchRow(row, patch)
  editingKey.value = null
}

async function acceptAll(): Promise<void> {
  const sets = new Map<string, ReviewRow>()
  for (const row of rows.value) {
    if (row.item.status === 'pending') sets.set(`${row.scope}/${row.kbName}`, row)
  }
  if (!sets.size) {
    showToast('没有待审条目', 'error')
    return
  }
  try {
    for (const row of sets.values()) {
      await client.acceptAll(row.scope, row.kbName, row.setName)
    }
    await reloadQueue()
    showToast('✓ 已全部采纳')
  } catch (caught) {
    showToast(caught instanceof Error ? caught.message : '全部采纳失败', 'error')
  }
}

async function reloadQueue(): Promise<void> {
  const sets = new Map<string, ReviewRow>()
  for (const row of rows.value) sets.set(`${row.scope}/${row.kbName}`, row)
  const merged: ReviewRow[] = []
  for (const row of sets.values()) {
    merged.push(...(await loadSetRows(row.scope, row.kbName, row.setName)))
  }
  rows.value = merged
}

function goBatch(): void {
  const first = rows.value[0]
  if (!first) {
    showToast('审核队列为空', 'error')
    return
  }
  router.push(
    `/platform/knowledge/${first.scope}/${first.kbName}/test?tab=batch&set=${first.setName}`,
  )
}

async function confirmToEval(): Promise<void> {
  if (!evalTaskId.value.trim() || !evalDataset.value.trim()) {
    showToast('任务 ID 与数据集名均不能为空', 'error')
    return
  }
  const sets = new Map<string, ReviewRow>()
  for (const row of rows.value) {
    if (row.item.status === 'accepted') sets.set(`${row.scope}/${row.kbName}`, row)
  }
  if (!sets.size) {
    showToast('没有已采纳条目可沉淀', 'error')
    return
  }
  evalBusy.value = true
  try {
    let appended = 0
    let skipped = 0
    for (const row of sets.values()) {
      const result = await client.toEvalDataset(row.scope, row.kbName, row.setName, {
        task_id: evalTaskId.value.trim(),
        dataset: evalDataset.value.trim(),
      })
      appended += result.appended
      skipped += result.skipped
    }
    showToast(`✓ 已沉淀 ${appended} 条 EvalCase（跳过重复 ${skipped} 条）`)
    evalModal.value = false
  } catch (caught) {
    showToast(caught instanceof Error ? caught.message : '沉淀失败', 'error')
  } finally {
    evalBusy.value = false
  }
}

function exportAccepted(): void {
  const accepted = rows.value
    .filter((row) => row.item.status === 'accepted')
    .map((row) => ({
      knowledge_base: row.kbName,
      question_set: row.setName,
      ...row.item,
    }))
  if (!accepted.length) {
    showToast('没有已采纳条目可导出', 'error')
    return
  }
  const blob = new Blob([JSON.stringify(accepted, null, 2)], {
    type: 'application/json;charset=utf-8',
  })
  const link = document.createElement('a')
  link.href = URL.createObjectURL(blob)
  link.download = 'question_set_accepted.json'
  link.click()
  URL.revokeObjectURL(link.href)
}

async function load(): Promise<void> {
  try {
    bases.value = await client.listBases()
    const valid = bases.value.filter((kb) => kb.valid)
    selectedSources.value = new Set(valid.slice(0, 2).map(sourceOf))
  } catch (caught) {
    showToast(caught instanceof Error ? caught.message : '知识库列表加载失败', 'error')
  }
  const queryScope = route.query.scope
  const queryName = route.query.name
  const querySet = route.query.set
  if (
    typeof queryScope === 'string' &&
    typeof queryName === 'string' &&
    typeof querySet === 'string'
  ) {
    rows.value = await loadSetRows(queryScope, queryName, querySet)
    queueNotice.value = `已载入问题集 ${querySet}`
  }
}

onMounted(() => void load())
onBeforeUnmount(() => {
  clearTimeout(pollTimer)
  pollController.abort()
})
</script>

<template>
  <div class="platform-root">
    <div class="kb-crumb">
      <RouterLink to="/platform/knowledge" class="kb-crumb-link">← 知识库</RouterLink>
      <b>生成评测集</b>
      <span class="platform-badge platform-b-amber">AI 生成 + 人工审核</span>
    </div>

    <div class="kb-gen-config">
      <div class="kb-gen-kbs">
        <div class="kb-gen-label">源知识库</div>
        <label v-for="kb in bases" :key="sourceOf(kb)" class="kb-check">
          <input
            type="checkbox"
            :checked="selectedSources.has(sourceOf(kb))"
            :disabled="generating"
            @change="toggleSource(kb, ($event.target as HTMLInputElement).checked)"
          />
          {{ kb.name }}
          <span class="platform-dim" style="font-size: 11px">{{
            kb.scope === 'global' ? '全局' : kb.scope
          }}</span>
        </label>
        <span v-if="!bases.length" class="platform-dim" style="font-size: 12px"
          >加载中…</span
        >
      </div>
      <div class="kb-gen-guide">
        <div class="kb-gen-label">指导要求（可定制）</div>
        <textarea
          v-model="guidance"
          rows="5"
          :disabled="generating"
          placeholder="描述业务背景和想要什么样的评测 case"
          aria-label="指导要求"
        ></textarea>
        <div style="display: flex; gap: 10px; align-items: center; margin-top: 8px">
          <label class="platform-mut" style="font-size: 12px">生成条数</label>
          <input
            v-model.number="count"
            type="number"
            min="1"
            max="100"
            style="width: 90px"
            :disabled="generating"
            aria-label="生成条数"
          />
        </div>
      </div>
      <div class="kb-gen-action">
        <button
          class="platform-btn platform-btn-pri"
          style="width: 100%"
          :disabled="generating || !selectedSources.size"
          data-testid="run-generate"
          @click="runGenerate"
        >
          {{ generating ? '生成中…' : '生成候选 case' }}
        </button>
        <div class="platform-dim" style="font-size: 11px; margin-top: 8px">
          逐文档生成 + 自动去重 + 多样性检查；结果全部进入下方审核队列，采纳后才入集
        </div>
      </div>
    </div>

    <div v-if="jobRunning" class="kb-gen-jobline">
      <span class="platform-badge platform-b-cyan"
        ><span class="platform-dot" />生成 Job {{ job?.job_id }} 运行中，每
        {{ POLL_INTERVAL_MS / 1000 }} 秒自动刷新…</span
      >
    </div>
    <div v-if="generateError" class="kb-gen-jobline">
      <span class="platform-field-error" role="alert" style="margin: 0"
        >✕ {{ generateError }}</span
      >
    </div>
    <div
      v-if="job && !jobRunning && job.results.length"
      class="kb-gen-jobline kb-gen-results"
    >
      <span v-for="result in job.results" :key="result.source" class="platform-mut">
        <span class="platform-mono">{{ result.source }}</span
        >：
        <template v-if="result.status === 'succeeded'"
          >生成 {{ result.generated }} · 保留 {{ result.kept }} · 问题集
          <span class="platform-mono">{{ result.question_set }}</span>
          <template v-if="Object.keys(result.shortfall).length">
            · 不足
            {{
              Object.entries(result.shortfall)
                .map(([type, n]) => `${CASE_TYPE_META[type]?.label ?? type} ${n}`)
                .join('、')
            }}
          </template>
        </template>
        <template v-else
          ><span style="color: var(--p-red)">失败：{{ result.error }}</span></template
        >
      </span>
    </div>

    <div class="kb-gen-queue">
      <div class="platform-card kb-queue-bar" data-testid="review-summary">
        <b>审核队列</b>
        <span
          >候选 <b>{{ summaryCounts.total }}</b></span
        >
        <span class="kb-hit-yes">已采纳 {{ summaryCounts.accepted }}</span>
        <span class="kb-hit-no">已丢弃 {{ summaryCounts.discarded }}</span>
        <span class="platform-mut">待审 {{ summaryCounts.pending }}</span>
        <span style="margin-left: auto"></span>
        <button
          class="platform-btn platform-btn-ghost platform-btn-sm"
          :disabled="!summaryCounts.pending"
          data-testid="accept-all"
          @click="acceptAll"
          >全部采纳</button
        >
        <button
          class="platform-btn platform-btn-sm platform-btn-pri"
          :disabled="!summaryCounts.total"
          @click="goBatch"
          >存入问题集并去批测 →</button
        >
        <button
          class="platform-btn platform-btn-ghost platform-btn-sm"
          :disabled="!summaryCounts.accepted"
          @click="evalModal = true"
          >沉淀为评测集</button
        >
        <button
          class="platform-btn platform-btn-ghost platform-btn-sm"
          :disabled="!summaryCounts.accepted"
          @click="exportAccepted"
          >导出文件（对外用）</button
        >
      </div>

      <p v-if="queueNotice" class="platform-mut" style="font-size: 12.5px">
        {{ queueNotice }}
      </p>
      <div v-if="!rows.length && !queueNotice" class="platform-state">
        选择源知识库并生成候选，或从批测页/问题集链接进入审核已有集合
      </div>

      <div
        v-for="row in rows"
        :key="rowKey(row)"
        class="platform-card kb-draft"
        :class="{
          'kb-draft-accepted': row.item.status === 'accepted',
          'kb-draft-discarded': row.item.status === 'discarded',
        }"
        :data-status="row.item.status"
      >
        <div class="kb-draft-main">
          <template v-if="editingKey === rowKey(row)">
            <div class="platform-field">
              <label>问题</label>
              <textarea v-model="editQuestion" rows="2" aria-label="编辑问题" />
            </div>
            <div class="platform-field">
              <label>期望 doc_id（| 分隔）</label>
              <input
                v-model="editDocIds"
                type="text"
                class="platform-mono"
                aria-label="编辑期望 doc_id"
              />
            </div>
            <div class="platform-field" style="margin-bottom: 0">
              <label>参考答案（可选）</label>
              <textarea v-model="editAnswer" rows="2" aria-label="编辑参考答案" />
            </div>
          </template>
          <template v-else>
            <div>
              <span
                class="platform-badge"
                :class="CASE_TYPE_META[row.item.case_type]?.cls ?? 'platform-b-mut'"
                >{{
                  CASE_TYPE_META[row.item.case_type]?.label ?? row.item.case_type
                }}</span
              >
              <b style="margin-left: 8px">{{ row.item.question }}</b>
              <span
                v-if="row.item.edited"
                class="platform-badge platform-b-violet"
                style="margin-left: 6px"
                >已编辑</span
              >
            </div>
            <div class="platform-mut" style="font-size: 12px; margin-top: 4px">
              {{ expectedLine(row.item) }} ·
              <span class="platform-mono">{{ row.kbName }}/{{ row.setName }}</span>
            </div>
          </template>
        </div>
        <div class="kb-draft-actions">
          <template v-if="editingKey === rowKey(row)">
            <button
              class="platform-btn platform-btn-sm platform-btn-pri"
              data-testid="save-edit"
              @click="saveEdit(row)"
              >保存</button
            >
            <button
              class="platform-btn platform-btn-ghost platform-btn-sm"
              @click="editingKey = null"
              >取消</button
            >
          </template>
          <template v-else-if="row.item.status === 'accepted'">
            <span class="kb-hit-yes" style="font-size: 12px">已采纳</span>
            <button
              class="platform-btn platform-btn-ghost platform-btn-sm"
              @click="startEdit(row)"
              >编辑</button
            >
            <button
              class="platform-btn platform-btn-ghost platform-btn-sm"
              @click="patchRow(row, { status: 'pending' })"
              >撤销</button
            >
          </template>
          <template v-else-if="row.item.status === 'discarded'">
            <span class="kb-hit-no" style="font-size: 12px">已丢弃</span>
            <button
              class="platform-btn platform-btn-ghost platform-btn-sm"
              @click="patchRow(row, { status: 'pending' })"
              >撤销</button
            >
          </template>
          <template v-else>
            <button
              class="platform-btn platform-btn-sm platform-btn-pri"
              data-testid="accept-item"
              @click="patchRow(row, { status: 'accepted' })"
              >采纳</button
            >
            <button
              class="platform-btn platform-btn-ghost platform-btn-sm"
              @click="startEdit(row)"
              >编辑</button
            >
            <button
              class="platform-btn platform-btn-ghost platform-btn-sm platform-btn-dngr"
              @click="patchRow(row, { status: 'discarded' })"
              >丢弃</button
            >
          </template>
        </div>
      </div>

      <p
        v-if="rows.length"
        class="platform-dim"
        style="font-size: 12px; margin-top: 10px"
      >
        「编辑」可修改问题文本/期望
        doc_id/参考答案后再采纳。采纳项自动保存在问题集文件中——「存入问题集并去批测」直接跳转批测页并选中该集合（无需导出文件再上传）；「沉淀为评测集」把采纳项转成带
        expected 的正式 EvalCase，进入 /platform/evaluation 做 Agent 级端到端评测。
      </p>
    </div>

    <Teleport to="body">
      <div v-if="evalModal" class="platform-modal-mask" @click.self="evalModal = false">
        <div class="platform-modal" role="dialog" aria-label="沉淀为评测集">
          <h3>沉淀为评测集</h3>
          <p>
            把已采纳条目转为 EvalCase，追加进
            eval_tasks/&lt;task&gt;/datasets/&lt;dataset&gt;.json，同 id 自动去重。
          </p>
          <label>评测任务 ID（eval_tasks/ 下的目录名）</label>
          <input
            v-model="evalTaskId"
            type="text"
            class="platform-mono"
            placeholder="auto_smoke"
            aria-label="评测任务 ID"
          />
          <label style="margin-top: 12px">数据集名（不含 .json）</label>
          <input
            v-model="evalDataset"
            type="text"
            class="platform-mono"
            placeholder="kb_generated"
            aria-label="数据集名"
          />
          <div class="platform-modal-actions">
            <button
              class="platform-btn platform-btn-sm"
              :disabled="evalBusy"
              @click="evalModal = false"
              >取消</button
            >
            <button
              class="platform-btn platform-btn-sm platform-btn-pri"
              :disabled="evalBusy"
              @click="confirmToEval"
            >
              {{ evalBusy ? '沉淀中…' : '确认沉淀' }}
            </button>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
.kb-crumb {
  flex: none;
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 24px;
  border-bottom: 1px solid var(--p-border);
  background: rgba(6, 9, 18, 0.6);
  font-size: 13px;
}
.kb-crumb-link {
  color: var(--p-cyan);
  text-decoration: none;
}
.kb-gen-config {
  flex: none;
  display: flex;
  gap: 20px;
  padding: 16px 24px;
  border-bottom: 1px solid var(--p-border);
  background: rgba(6, 9, 18, 0.35);
  align-items: flex-start;
}
.kb-gen-kbs {
  width: 200px;
  flex-shrink: 0;
}
.kb-gen-guide {
  flex: 1;
  min-width: 0;
}
.kb-gen-action {
  width: 220px;
  flex-shrink: 0;
}
.kb-gen-label {
  font-size: 12px;
  color: var(--p-mut);
  margin-bottom: 6px;
}
.kb-check {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  margin-bottom: 6px;
}
.kb-check input {
  width: auto;
  accent-color: var(--p-cyan);
}
.kb-gen-jobline {
  flex: none;
  padding: 10px 24px 0;
}
.kb-gen-results {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 12.5px;
}
.kb-gen-queue {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 16px 24px 60px;
}
.kb-queue-bar {
  padding: 12px 16px;
  display: flex;
  gap: 18px;
  align-items: center;
  position: sticky;
  top: -16px;
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
.kb-draft {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 12px 16px;
  margin-top: 10px;
}
.kb-draft-accepted {
  border-color: rgba(52, 211, 153, 0.45);
}
.kb-draft-discarded {
  opacity: 0.5;
}
.kb-draft-main {
  flex: 1;
  min-width: 0;
}
.kb-draft-actions {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-shrink: 0;
}
</style>
