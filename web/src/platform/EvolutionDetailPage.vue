<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import BarChart from './components/evolution/BarChart.vue'
import CandidatePipeline, {
  type PipelineCandidate,
  type PipelineStageState,
} from './components/evolution/CandidatePipeline.vue'
import CaseHeatmap, { type HeatmapColumn } from './components/evolution/CaseHeatmap.vue'
import TrendChart, { type TrendPoint } from './components/evolution/TrendChart.vue'
import {
  defaultEvolutionClient,
  type EvolutionCandidate,
  type EvolutionProgressEvent,
  type EvolutionRoleResult,
  type EvolutionRunView,
  type EvolutionTaskDetail,
} from './evolutionClient'
import { showToast } from './toast'

/* ---------- 类型 ---------- */

interface LiveCandidate {
  id: string
  parent: string | null
  isRollback: boolean
  stages: PipelineStageState[]
  trainScore: number | null
  valScore: number | null
  failed: boolean
  note: string
}

interface CaseRow {
  name: string
  state: 'running' | 'done'
  result: string | null
}

type StagePanel =
  | { kind: 'idle'; title: string; sub: string }
  | { kind: 'improve'; candidate: string; attempt: string; maxAttempts: string }
  | { kind: 'cases'; candidate: string; role: 'train' | 'validation'; cases: CaseRow[] }

interface LogLine {
  ts: number
  kind: 'info' | 'warn' | 'err'
  text: string
}

interface ChangelogSection {
  title: string
  body: string
}

interface ChangelogGroup {
  heading: string
  sections: ChangelogSection[]
}

const STAGE_COUNT = 5
const MAX_LOG_LINES = 500

/* ---------- 基础状态 ---------- */

const client = defaultEvolutionClient()
const route = useRoute()
const router = useRouter()
const taskId = String(route.params.id)

const detail = ref<EvolutionTaskDetail | null>(null)
const error = ref<string | null>(null)
const run = ref<EvolutionRunView | null>(null)
const starting = ref(false)
const stopConfirm = ref(false)
const stopBusy = ref(false)

/* ---------- 实时监控状态 ---------- */

const liveCandidates = ref<LiveCandidate[]>([])
const currentIteration = ref('')
const currentRole = ref<'train' | 'validation'>('train')
const currentCandidateId = ref<string | null>(null)
const stagePanel = ref<StagePanel>({ kind: 'idle', title: '初始化', sub: '等待事件…' })
const logs = ref<LogLine[]>([])
const liveTokens = ref(0)
const logBox = ref<HTMLElement | null>(null)

let sseController: AbortController | null = null
let realignTimer: ReturnType<typeof setTimeout> | null = null

const isLive = computed(
  () =>
    run.value !== null &&
    (run.value.status === 'starting' || run.value.status === 'running'),
)

const viewMode = computed<'loading' | 'idle' | 'live' | 'final'>(() => {
  if (!detail.value) return 'loading'
  if (isLive.value) return 'live'
  if (detail.value.candidates.length === 0) return 'idle'
  return 'final'
})

const runStatusMeta = computed(() => {
  const status = run.value?.status
  if (status === 'running' || status === 'starting')
    return { label: 'RUNNING', cls: 'platform-b-cyan' }
  if (status === 'completed') return { label: 'COMPLETED', cls: 'platform-b-green' }
  if (status === 'failed') return { label: 'FAILED', cls: 'platform-b-red' }
  if (status === 'stopped') return { label: 'STOPPED', cls: 'platform-b-amber' }
  return { label: '未运行', cls: 'platform-b-mut' }
})

/* ---------- 实时派生 ---------- */

const liveBestId = computed(() => {
  let bestId: string | null = null
  let bestScore = -1
  for (const candidate of liveCandidates.value) {
    if (candidate.valScore !== null && candidate.valScore > bestScore) {
      bestScore = candidate.valScore
      bestId = candidate.id
    }
  }
  return bestId
})

const liveBestScore = computed(() => {
  const best = liveCandidates.value.find((c) => c.id === liveBestId.value)
  return best?.valScore ?? null
})

const liveBestTrain = computed(() => {
  let best: number | null = null
  for (const candidate of liveCandidates.value) {
    if (
      candidate.trainScore !== null &&
      (best === null || candidate.trainScore > best)
    ) {
      best = candidate.trainScore
    }
  }
  return best
})

const pipelineCandidates = computed<PipelineCandidate[]>(() =>
  liveCandidates.value.map((candidate) => ({
    id: candidate.id,
    stages: candidate.stages,
    active: candidate.stages.includes('run'),
    best: candidate.id === liveBestId.value,
    badges: [
      ...(candidate.parent === null && candidate.stages[1] === 'ok'
        ? [{ label: 'seed', cls: 'platform-b-mut' }]
        : []),
      ...(candidate.isRollback
        ? [{ label: 'rollback', cls: 'platform-b-violet' }]
        : []),
      ...(candidate.id === liveBestId.value
        ? [{ label: 'BEST', cls: 'platform-b-amber' }]
        : []),
      ...(candidate.failed ? [{ label: 'FAILED', cls: 'platform-b-red' }] : []),
    ],
    scoreLine: liveScoreLine(candidate),
    note: candidate.note || undefined,
  })),
)

const liveTrendPoints = computed<TrendPoint[]>(() =>
  liveCandidates.value.map((candidate) => ({
    train: candidate.trainScore,
    validation: candidate.valScore,
  })),
)

function liveScoreLine(candidate: LiveCandidate): string {
  const parts: string[] = []
  if (candidate.trainScore !== null)
    parts.push(`train ${(candidate.trainScore * 100).toFixed(1)}%`)
  if (candidate.valScore !== null)
    parts.push(`val ${(candidate.valScore * 100).toFixed(1)}%`)
  return parts.join(' · ')
}

const casePanelProgress = computed(() => {
  const panel = stagePanel.value
  if (panel.kind !== 'cases' || panel.cases.length === 0) return null
  const done = panel.cases.filter((row) => row.state === 'done').length
  return { done, total: panel.cases.length, pct: (done / panel.cases.length) * 100 }
})

/* ---------- 终态派生 ---------- */

const finalCandidates = computed<EvolutionCandidate[]>(
  () => detail.value?.candidates ?? [],
)

const finalBestId = computed(() => {
  if (run.value?.best_candidate_id) return run.value.best_candidate_id
  let bestId: string | null = null
  let bestScore = -1
  for (const candidate of finalCandidates.value) {
    const score = candidate.validation?.total_score
    if (score !== undefined && score > bestScore) {
      bestScore = score
      bestId = candidate.candidate_id
    }
  }
  return bestId
})

const finalBestValidation = computed(
  () =>
    run.value?.validation_score ??
    finalCandidates.value.find((c) => c.candidate_id === finalBestId.value)?.validation
      ?.total_score ??
    null,
)

const finalBestTrain = computed(() => {
  let best: number | null = null
  for (const candidate of finalCandidates.value) {
    const score = candidate.train?.total_score
    if (score !== undefined && (best === null || score > best)) best = score
  }
  return best
})

const finalTotalTokens = computed(() =>
  finalCandidates.value.reduce((sum, candidate) => sum + candidateTokens(candidate), 0),
)

const finalTrendPoints = computed<TrendPoint[]>(() =>
  finalCandidates.value.map((candidate) => ({
    train: candidate.train?.total_score ?? null,
    validation: candidate.validation?.total_score ?? null,
  })),
)

const finalTokenBars = computed(() =>
  finalCandidates.value.map((candidate) => candidateTokens(candidate)),
)

const finalDurationBars = computed(() =>
  finalCandidates.value.map((candidate) => candidateDuration(candidate) / 60),
)

const heatmapCaseIds = computed(() => {
  const ids: string[] = []
  for (const candidate of finalCandidates.value) {
    for (const entry of candidate.train?.cases ?? []) {
      if (!ids.includes(entry.case_id)) ids.push(entry.case_id)
    }
  }
  return ids
})

const heatmapColumns = computed<HeatmapColumn[]>(() =>
  finalCandidates.value.map((candidate, index) => {
    const results: Record<string, number> = {}
    for (const entry of candidate.train?.cases ?? [])
      results[entry.case_id] = entry.score
    return { label: `c${index}`, results }
  }),
)

function candidateTokens(candidate: EvolutionCandidate): number {
  return (
    (candidate.train?.total_tokens ?? 0) + (candidate.validation?.total_tokens ?? 0)
  )
}

function candidateDuration(candidate: EvolutionCandidate): number {
  return (
    (candidate.train?.duration_seconds ?? 0) +
    (candidate.validation?.duration_seconds ?? 0)
  )
}

/* ---------- changelog 解析与按需加载 ---------- */

function parseChangelog(markdown: string): ChangelogGroup[] | null {
  const groups: ChangelogGroup[] = []
  let group: ChangelogGroup | null = null
  let section: ChangelogSection | null = null
  for (const line of markdown.split('\n')) {
    if (line.startsWith('## ')) {
      group = { heading: line.slice(3).trim(), sections: [] }
      groups.push(group)
      section = null
    } else if (line.startsWith('### ')) {
      if (!group) {
        group = { heading: '', sections: [] }
        groups.push(group)
      }
      section = { title: line.slice(4).trim(), body: '' }
      group.sections.push(section)
    } else if (section) {
      section.body += `${line}\n`
    } else if (group && line.trim()) {
      section = { title: '', body: `${line}\n` }
      group.sections.push(section)
    }
  }
  return groups.length > 0 ? groups : null
}

interface CachedChangelog {
  markdown: string
  groups: ChangelogGroup[] | null
}

/** 展开状态、内容缓存与加载状态；同一候选只请求一次。 */
const expandedChangelogs = ref<Set<string>>(new Set())
const changelogs = ref<Record<string, CachedChangelog>>({})
const changelogLoading = ref<Set<string>>(new Set())
const changelogErrors = ref<Record<string, string>>({})

const allChangelogsExpanded = computed(
  () =>
    finalCandidates.value.length > 0 &&
    finalCandidates.value.every((c) => expandedChangelogs.value.has(c.candidate_id)),
)

function isChangelogExpanded(candidateId: string): boolean {
  return expandedChangelogs.value.has(candidateId)
}

async function ensureChangelog(candidateId: string): Promise<void> {
  if (changelogs.value[candidateId] || changelogLoading.value.has(candidateId)) return
  changelogLoading.value = new Set([...changelogLoading.value, candidateId])
  try {
    const markdown = await client.candidateChangelog(taskId, candidateId)
    changelogs.value = {
      ...changelogs.value,
      [candidateId]: { markdown, groups: parseChangelog(markdown) },
    }
    const errors = { ...changelogErrors.value }
    delete errors[candidateId]
    changelogErrors.value = errors
  } catch (caught) {
    const message = caught instanceof Error ? caught.message : 'changelog 加载失败'
    changelogErrors.value = { ...changelogErrors.value, [candidateId]: message }
    showToast(message, 'error')
  } finally {
    const loading = new Set(changelogLoading.value)
    loading.delete(candidateId)
    changelogLoading.value = loading
  }
}

async function toggleChangelog(candidateId: string): Promise<void> {
  const next = new Set(expandedChangelogs.value)
  if (next.has(candidateId)) {
    next.delete(candidateId)
    expandedChangelogs.value = next
    return
  }
  next.add(candidateId)
  expandedChangelogs.value = next
  await ensureChangelog(candidateId)
}

async function toggleAllChangelogs(): Promise<void> {
  if (allChangelogsExpanded.value) {
    expandedChangelogs.value = new Set()
    return
  }
  expandedChangelogs.value = new Set(finalCandidates.value.map((c) => c.candidate_id))
  await Promise.all(finalCandidates.value.map((c) => ensureChangelog(c.candidate_id)))
}

function summarizeMarkdown(markdown: string): string {
  const groups = parseChangelog(markdown)
  const first = groups?.flatMap((g) => g.sections)[0]
  const text = (first ? `${first.title} ${first.body}` : markdown)
    .replace(/\s+/g, ' ')
    .trim()
  if (!text) return '—'
  return text.length > 80 ? `${text.slice(0, 80)}…` : text
}

/** 折叠态摘要行：未拉取时提示展开，已拉取则复用解析摘要。 */
function changelogSummaryText(candidateId: string): string {
  const cached = changelogs.value[candidateId]
  return cached ? summarizeMarkdown(cached.markdown) : '点击展开查看改进策略'
}

/** 对比表摘要列：未拉取时不强行请求。 */
function changelogSummaryCell(candidateId: string): string {
  const cached = changelogs.value[candidateId]
  return cached ? summarizeMarkdown(cached.markdown) : '—'
}

/* ---------- 格式化 ---------- */

function pct(value: number | null): string {
  return value === null ? '—' : `${(value * 100).toFixed(1)}%`
}

function roleResultText(result: EvolutionRoleResult | null): string {
  if (!result) return '—'
  return `${pct(result.total_score)} | ${result.correct}/${result.case_count}`
}

function formatDuration(seconds: number): string {
  if (seconds <= 0) return '—'
  const minutes = Math.floor(seconds / 60)
  const rest = Math.round(seconds % 60)
  return `${minutes}m ${String(rest).padStart(2, '0')}s`
}

function formatTokens(tokens: number): string {
  if (tokens <= 0) return '—'
  if (tokens >= 1000) return `${(tokens / 1000).toFixed(1)}k`
  return String(tokens)
}

function formatLogTime(ts: number): string {
  if (!ts) return '--:--:--'
  return new Date(ts * 1000).toTimeString().slice(0, 8)
}

function candidateStatusMeta(candidate: EvolutionCandidate): {
  label: string
  cls: string
} {
  if (candidate.candidate_id === finalBestId.value)
    return { label: '★ 验证最佳', cls: 'platform-b-amber' }
  if (!candidate.train) return { label: 'FAILED', cls: 'platform-b-red' }
  if (!candidate.validation) return { label: 'VAL 跳过', cls: 'platform-b-mut' }
  return { label: 'DONE', cls: 'platform-b-mut' }
}

/* ---------- 日志 ---------- */

function pushLog(kind: LogLine['kind'], text: string): void {
  logs.value.push({ ts: Date.now() / 1000, kind, text })
  if (logs.value.length > MAX_LOG_LINES) {
    logs.value.splice(0, logs.value.length - MAX_LOG_LINES)
  }
}

watch(
  () => logs.value.length,
  async () => {
    await nextTick()
    const el = logBox.value
    if (el) el.scrollTop = el.scrollHeight
  },
)

/* ---------- SSE 事件 → 状态映射 ---------- */

function findOrCreateCandidate(id: string): LiveCandidate {
  let candidate = liveCandidates.value.find((c) => c.id === id)
  if (!candidate) {
    candidate = {
      id,
      parent: null,
      isRollback: false,
      stages: Array.from(
        { length: STAGE_COUNT },
        () => 'pending' as PipelineStageState,
      ),
      trainScore: null,
      valScore: null,
      failed: false,
      note: '',
    }
    liveCandidates.value.push(candidate)
  }
  return candidate
}

function stageIndex(role: string, stage: string): number {
  if (role === 'validation') return 4
  return stage === 'evaluation' ? 3 : 2
}

function setStage(candidateId: string, index: number, state: PipelineStageState): void {
  const candidate = findOrCreateCandidate(candidateId)
  candidate.stages[index] = state
}

function ensureCasePanel(candidateId: string): CaseRow[] {
  const panel = stagePanel.value
  if (panel.kind === 'cases' && panel.candidate === candidateId) return panel.cases
  const next: StagePanel = {
    kind: 'cases',
    candidate: candidateId,
    role: currentRole.value,
    cases: [],
  }
  stagePanel.value = next
  return next.cases
}

function handleCaseRow(candidateId: string, caseName: string): CaseRow {
  const rows = ensureCasePanel(candidateId)
  let row = rows.find((r) => r.name === caseName)
  if (!row) {
    row = { name: caseName, state: 'running', result: null }
    rows.push(row)
  }
  return row
}

function onStageCompleted(fields: Record<string, string>): void {
  const candidateId = fields.candidate ?? currentCandidateId.value
  if (!candidateId) return
  const role = fields.role ?? currentRole.value
  const stage = fields.stage ?? 'execution'
  const candidate = findOrCreateCandidate(candidateId)
  if (stage === 'execution') {
    candidate.stages[stageIndex(role, 'execution')] = 'ok'
    if (role === 'train') candidate.stages[3] = 'run'
    return
  }
  const score = Number.parseFloat(fields.score ?? '')
  candidate.stages[stageIndex(role, 'evaluation')] = 'ok'
  if (role === 'train') {
    if (Number.isFinite(score)) candidate.trainScore = score
    currentRole.value = 'validation'
    stagePanel.value = { kind: 'idle', title: '训练评分完成', sub: '等待验证…' }
  } else if (Number.isFinite(score)) {
    candidate.valScore = score
  }
  scheduleRealign()
}

function handleEvent(event: EvolutionProgressEvent): void {
  const fields = event.fields
  switch (event.event) {
    case 'run_started':
      pushLog('info', event.message)
      break
    case 'iteration_started': {
      currentIteration.value = fields.iteration ?? ''
      const id = fields.candidate ?? ''
      if (id) {
        findOrCreateCandidate(id)
        currentCandidateId.value = id
      }
      pushLog('info', event.message)
      break
    }
    case 'improvement_sources':
    case 'diagnosis_brief_built':
      pushLog('info', event.message)
      break
    case 'improver_attempt_started': {
      const id = fields.candidate ?? currentCandidateId.value ?? ''
      if (id) {
        setStage(id, 0, 'run')
        currentCandidateId.value = id
        stagePanel.value = {
          kind: 'improve',
          candidate: id,
          attempt: fields.attempt ?? '?',
          maxAttempts: fields.max_attempts ?? '?',
        }
      }
      pushLog('info', event.message)
      break
    }
    case 'improver_attempt_failed':
      pushLog('warn', event.message)
      break
    case 'candidate_published': {
      const id = fields.candidate ?? currentCandidateId.value ?? ''
      if (id) {
        const candidate = findOrCreateCandidate(id)
        candidate.stages[0] = 'ok'
        candidate.stages[1] = 'ok'
        candidate.parent = fields.parent ?? null
      }
      pushLog('info', event.message)
      break
    }
    case 'candidate_not_published': {
      const id = fields.output_candidate ?? fields.candidate ?? ''
      if (id) {
        const candidate = findOrCreateCandidate(id)
        candidate.stages[0] = 'fail'
        candidate.failed = true
      }
      pushLog('err', event.message)
      break
    }
    case 'candidate_restored': {
      const id = fields.candidate ?? ''
      if (id) findOrCreateCandidate(id).isRollback = true
      pushLog('info', event.message)
      break
    }
    case 'candidate_started': {
      const id = fields.candidate ?? currentCandidateId.value ?? ''
      if (id) {
        currentCandidateId.value = id
        currentRole.value = 'train'
        setStage(id, 2, 'run')
        stagePanel.value = { kind: 'cases', candidate: id, role: 'train', cases: [] }
      }
      pushLog('info', event.message)
      break
    }
    case 'stage_resuming':
    case 'stage_reused': {
      const id = fields.candidate ?? currentCandidateId.value ?? ''
      const role = fields.role === 'validation' ? 'validation' : 'train'
      if (id) {
        currentCandidateId.value = id
        currentRole.value = role
        setStage(id, stageIndex(role, fields.stage ?? 'execution'), 'run')
        stagePanel.value = { kind: 'cases', candidate: id, role, cases: [] }
      }
      pushLog('info', event.message)
      break
    }
    case 'case_dispatched': {
      const id = currentCandidateId.value
      const caseName = fields.case
      if (id && caseName) {
        if (currentRole.value === 'validation') setStage(id, 4, 'run')
        handleCaseRow(id, caseName)
      }
      break
    }
    case 'case_retry': {
      const id = currentCandidateId.value
      const caseName = fields.case
      if (id && caseName) {
        const row = handleCaseRow(id, caseName)
        row.state = 'running'
      }
      pushLog('warn', event.message)
      break
    }
    case 'case_completed': {
      const id = currentCandidateId.value
      const caseName = fields.case
      if (id && caseName) {
        const row = handleCaseRow(id, caseName)
        row.state = 'done'
        row.result = fields.result ?? null
      }
      break
    }
    case 'stage_completed':
      onStageCompleted(fields)
      pushLog('info', event.message)
      break
    case 'validation_skipped': {
      const id = fields.candidate ?? currentCandidateId.value ?? ''
      if (id) {
        const candidate = findOrCreateCandidate(id)
        candidate.stages[4] = 'skip'
        candidate.note = `validation_skipped · ${fields.reason ?? ''}`.trim()
      }
      stagePanel.value = { kind: 'idle', title: '验证跳过', sub: fields.reason ?? '' }
      pushLog('warn', event.message)
      break
    }
    case 'run_completed':
      pushLog('info', event.message)
      void finishRun('completed')
      break
    case 'run_failed':
      pushLog('err', event.message)
      void finishRun('failed')
      break
    case 'run_stopped':
      pushLog('warn', event.message)
      void finishRun('stopped')
      break
    default:
      pushLog('info', event.message || event.event)
  }
}

/* ---------- SSE 生命周期 ---------- */

function stopSse(): void {
  sseController?.abort()
  sseController = null
}

function openEvents(runId: string): void {
  stopSse()
  sseController = new AbortController()
  client.openRunEvents(runId, {
    signal: sseController.signal,
    onEvent: handleEvent,
    onReconnect: () => void realignFromDetail(),
    onError: (caught) => pushLog('err', `事件流断开：${caught.message}`),
  })
}

function seedLiveFromDetail(): void {
  liveCandidates.value = (detail.value?.candidates ?? []).map((candidate) => ({
    id: candidate.candidate_id,
    parent: candidate.parent_id,
    isRollback: candidate.is_rollback,
    stages: [
      'ok',
      'ok',
      candidate.train ? 'ok' : 'pending',
      candidate.train ? 'ok' : 'pending',
      candidate.validation ? 'ok' : candidate.train ? 'skip' : 'pending',
    ] as PipelineStageState[],
    trainScore: candidate.train?.total_score ?? null,
    valScore: candidate.validation?.total_score ?? null,
    failed: !candidate.train,
    note: candidate.validation || !candidate.train ? '' : 'validation_skipped',
  }))
  liveTokens.value = (detail.value?.candidates ?? []).reduce(
    (sum, candidate) => sum + candidateTokens(candidate),
    0,
  )
}

async function realignFromDetail(): Promise<void> {
  if (!isLive.value) return
  try {
    const latest = await client.taskDetail(taskId)
    detail.value = latest
    for (const candidate of latest.candidates) {
      const live = findOrCreateCandidate(candidate.candidate_id)
      live.parent = candidate.parent_id
      live.isRollback = candidate.is_rollback
      if (candidate.train) {
        live.trainScore = candidate.train.total_score
        live.stages[2] = 'ok'
        live.stages[3] = 'ok'
      }
      if (candidate.validation) {
        live.valScore = candidate.validation.total_score
        live.stages[4] = 'ok'
      } else if (candidate.train && live.stages[4] === 'pending') {
        live.stages[4] = 'skip'
      }
    }
    liveTokens.value = latest.candidates.reduce(
      (sum, candidate) => sum + candidateTokens(candidate),
      0,
    )
  } catch {
    // 对齐失败不影响事件流，下次重连或阶段完成时重试。
  }
}

function scheduleRealign(): void {
  if (realignTimer !== null) return
  realignTimer = setTimeout(() => {
    realignTimer = null
    void realignFromDetail()
  }, 800)
}

async function finishRun(status: 'completed' | 'failed' | 'stopped'): Promise<void> {
  if (realignTimer !== null) {
    clearTimeout(realignTimer)
    realignTimer = null
  }
  stopSse()
  if (run.value) run.value = { ...run.value, status }
  try {
    const runId = run.value?.run_id
    if (runId) run.value = await client.getRun(runId)
    detail.value = await client.taskDetail(taskId)
  } catch {
    // 终态视图使用已有数据兜底。
  }
}

/* ---------- 操作 ---------- */

async function startRun(): Promise<void> {
  starting.value = true
  try {
    run.value = await client.startRun(taskId)
    seedLiveFromDetail()
    stagePanel.value = { kind: 'idle', title: '初始化', sub: 'run 已启动，等待事件…' }
    openEvents(run.value.run_id)
    showToast('✓ 优化任务已启动')
  } catch (caught) {
    showToast(caught instanceof Error ? caught.message : '启动失败', 'error')
    await load()
  } finally {
    starting.value = false
  }
}

async function confirmStop(): Promise<void> {
  const target = run.value
  if (!target) return
  stopBusy.value = true
  try {
    run.value = await client.stopRun(target.run_id)
    showToast('✓ 已请求停止')
    stopConfirm.value = false
    if (!isLive.value)
      await finishRun(run.value.status === 'stopped' ? 'stopped' : 'completed')
  } catch (caught) {
    showToast(caught instanceof Error ? caught.message : '停止失败', 'error')
  } finally {
    stopBusy.value = false
  }
}

/* ---------- 加载 ---------- */

async function load(): Promise<void> {
  error.value = null
  try {
    detail.value = await client.taskDetail(taskId)
    const activeRunId = detail.value.active_run_id
    if (activeRunId) {
      run.value = await client.getRun(activeRunId)
      if (isLive.value) {
        seedLiveFromDetail()
        openEvents(activeRunId)
      }
    }
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : '任务详情加载失败'
  }
}

onMounted(() => void load())
onUnmounted(() => {
  stopSse()
  if (realignTimer !== null) clearTimeout(realignTimer)
})
</script>

<template>
  <div class="platform-root">
    <div class="platform-page">
      <div class="platform-page-inner">
        <div class="evo-mon-head">
          <button
            class="platform-btn platform-btn-sm"
            @click="router.push('/platform/evolution')"
            >← 任务列表</button
          >
          <h1>{{ taskId }}</h1>
          <span class="platform-badge" :class="runStatusMeta.cls">
            <span class="platform-dot" />{{ runStatusMeta.label }}
          </span>
          <div class="evo-run-meta platform-mono">
            <span v-if="run">run: {{ run.run_id }}</span>
            <span v-if="detail">target: {{ detail.initial_target }}</span>
            <span v-if="run?.stop_reason">stop_reason: {{ run.stop_reason }}</span>
          </div>
          <div style="margin-left: auto; display: flex; gap: 10px">
            <button
              v-if="isLive"
              class="platform-btn platform-btn-sm platform-btn-dngr"
              @click="stopConfirm = true"
            >
              ■ 停止任务
            </button>
          </div>
        </div>

        <div v-if="error" class="platform-state">
          <p role="alert">{{ error }}</p>
          <button class="platform-btn" @click="load">重试</button>
        </div>
        <div v-else-if="!detail" class="platform-state">正在加载任务详情…</div>

        <!-- ===== 未运行：配置摘要 + 启动 ===== -->
        <template v-else-if="viewMode === 'idle'">
          <div class="platform-card" style="padding: 24px 26px; max-width: 720px">
            <h3 style="margin: 0 0 14px; font-size: 15px">任务配置</h3>
            <div class="platform-sumrow"
              ><span>task_id</span
              ><b class="platform-mono">{{ detail.task_id }}</b></div
            >
            <div class="platform-sumrow"
              ><span>initial_target</span
              ><b class="platform-mono">{{ detail.initial_target }}</b></div
            >
            <div class="platform-sumrow"
              ><span>max_iterations</span
              ><b class="platform-mono">{{ detail.max_iterations }}</b></div
            >
            <div class="platform-sumrow" style="align-items: flex-start">
              <span>objective</span
              ><b style="max-width: 70%; text-align: right; font-weight: 400">{{
                detail.objective
              }}</b>
            </div>
            <p
              v-if="run?.status === 'failed' && run.error"
              class="platform-agent-card-error"
              style="margin-top: 12px"
            >
              ✕ 上次运行失败：{{ run.error }}
            </p>
            <div style="margin-top: 20px; display: flex; justify-content: flex-end">
              <button
                class="platform-btn platform-btn-pri"
                :disabled="starting"
                @click="startRun"
              >
                {{ starting ? '启动中…' : '▶ 启动优化任务' }}
              </button>
            </div>
          </div>
        </template>

        <!-- ===== 运行中：实时监控 ===== -->
        <template v-else-if="viewMode === 'live'">
          <div class="platform-stats" style="grid-template-columns: repeat(5, 1fr)">
            <div class="platform-card platform-stat">
              <div class="platform-stat-label">当前迭代</div>
              <div class="platform-stat-value" data-testid="stat-iteration">{{
                currentIteration || '—'
              }}</div>
            </div>
            <div class="platform-card platform-stat">
              <div class="platform-stat-label">最佳验证分</div>
              <div class="platform-stat-value" style="color: var(--p-amber)">{{
                pct(liveBestScore)
              }}</div>
            </div>
            <div class="platform-card platform-stat">
              <div class="platform-stat-label">最佳训练分</div>
              <div class="platform-stat-value" style="color: var(--p-green)">{{
                pct(liveBestTrain)
              }}</div>
            </div>
            <div class="platform-card platform-stat">
              <div class="platform-stat-label">候选版本</div>
              <div class="platform-stat-value" data-testid="stat-candidates">{{
                liveCandidates.length
              }}</div>
            </div>
            <div class="platform-card platform-stat">
              <div class="platform-stat-label">累计 Tokens</div>
              <div class="platform-stat-value">{{ formatTokens(liveTokens) }}</div>
            </div>
          </div>

          <div class="evo-mon-grid">
            <div class="platform-card evo-panel">
              <h3
                >候选版本流水线
                <span class="right">Improve → Train → Validate</span></h3
              >
              <CandidatePipeline :candidates="pipelineCandidates" />
            </div>

            <div style="display: flex; flex-direction: column; gap: 16px">
              <div class="platform-card evo-panel">
                <h3>评分趋势 <span class="right">total_score per candidate</span></h3>
                <TrendChart :points="liveTrendPoints" :best="liveBestScore" />
              </div>

              <div class="platform-card evo-panel">
                <h3 data-testid="stage-title">
                  <template v-if="stagePanel.kind === 'improve'"
                    >当前阶段 · 优化改进（Improvement）</template
                  >
                  <template v-else-if="stagePanel.kind === 'cases'">
                    当前阶段 ·
                    {{ stagePanel.role === 'train' ? '训练集执行' : '验证集执行' }}
                  </template>
                  <template v-else>{{ stagePanel.title }}</template>
                  <span class="right" data-testid="stage-right">
                    <template v-if="stagePanel.kind === 'improve'">
                      {{ stagePanel.candidate }} · improver attempt
                      {{ stagePanel.attempt }}/{{ stagePanel.maxAttempts }}
                    </template>
                    <template v-else-if="stagePanel.kind === 'cases'">
                      {{ stagePanel.candidate }} · {{ stagePanel.cases.length }} cases
                    </template>
                    <template v-else>{{ stagePanel.sub }}</template>
                  </span>
                </h3>

                <div
                  v-if="stagePanel.kind === 'improve'"
                  class="platform-mut"
                  style="font-size: 12.5px; padding: 8px 0"
                >
                  <span class="evo-spinner" style="margin-right: 8px" />
                  evolution-agent 正在分析失败证据并改写 Agent 配置（attempt
                  {{ stagePanel.attempt }}/{{ stagePanel.maxAttempts }}）…
                </div>

                <div v-else-if="stagePanel.kind === 'cases'">
                  <div v-if="casePanelProgress" class="evo-pbar">
                    <i :style="{ width: `${casePanelProgress.pct}%` }" />
                  </div>
                  <div
                    v-if="casePanelProgress"
                    class="platform-dim platform-mono"
                    style="font-size: 10.5px; margin-bottom: 6px"
                  >
                    {{ casePanelProgress.done }} / {{ casePanelProgress.total }} 完成
                  </div>
                  <div
                    v-for="row in stagePanel.cases"
                    :key="row.name"
                    class="evo-case-line"
                  >
                    <span v-if="row.state === 'running'" class="evo-spinner" />
                    <span
                      v-else-if="row.result === 'completed'"
                      style="color: var(--p-green)"
                      >✓</span
                    >
                    <span v-else style="color: var(--p-red)">✗</span>
                    <span class="cname">{{ row.name }}</span>
                    <span class="ctok">{{ row.result ?? '' }}</span>
                  </div>
                  <div
                    v-if="stagePanel.cases.length === 0"
                    class="platform-mut"
                    style="font-size: 12.5px; padding: 8px 0"
                  >
                    等待 case 调度…
                  </div>
                </div>

                <div
                  v-else
                  class="platform-mut"
                  style="font-size: 12.5px; padding: 8px 0"
                >
                  {{ stagePanel.sub || '等待下一阶段…' }}
                </div>
              </div>

              <div class="platform-card evo-panel">
                <h3
                  >实时事件日志
                  <span class="right">{{ run?.event_count ?? 0 }} events</span></h3
                >
                <div ref="logBox" class="evo-log-box" data-testid="log-box">
                  <div v-for="(line, index) in logs" :key="index">
                    <span class="lt">{{ formatLogTime(line.ts) }}</span>
                    <span
                      :class="
                        line.kind === 'err'
                          ? 'lv-err'
                          : line.kind === 'warn'
                            ? 'lv-warn'
                            : 'lv-info'
                      "
                    >
                      {{
                        line.kind === 'err'
                          ? 'ERR '
                          : line.kind === 'warn'
                            ? 'WARN'
                            : 'INFO'
                      }}
                    </span>
                    <span class="lm">{{ line.text }}</span>
                  </div>
                  <div v-if="logs.length === 0" class="platform-dim">等待事件…</div>
                </div>
              </div>
            </div>
          </div>
        </template>

        <!-- ===== 终态：完成视图 ===== -->
        <template v-else>
          <div class="platform-stats">
            <div class="platform-card platform-stat">
              <div class="platform-stat-label">最佳验证分</div>
              <div class="platform-stat-value" style="color: var(--p-amber)">{{
                pct(finalBestValidation)
              }}</div>
              <div class="platform-stat-sub">{{ finalBestId ?? '—' }}</div>
            </div>
            <div class="platform-card platform-stat">
              <div class="platform-stat-label">最佳训练分</div>
              <div class="platform-stat-value" style="color: var(--p-green)">{{
                pct(finalBestTrain)
              }}</div>
            </div>
            <div class="platform-card platform-stat">
              <div class="platform-stat-label">候选版本</div>
              <div class="platform-stat-value">{{ finalCandidates.length }}</div>
              <div class="platform-stat-sub"
                >含 seed · 上限 {{ detail.max_iterations }}</div
              >
            </div>
            <div class="platform-card platform-stat">
              <div class="platform-stat-label">累计 Tokens</div>
              <div class="platform-stat-value">{{
                finalTotalTokens.toLocaleString()
              }}</div>
            </div>
          </div>

          <div class="evo-mon-grid" style="grid-template-columns: 450px 1fr">
            <div class="platform-card evo-panel">
              <h3>
                每轮改进策略 <span class="right">changelog.md</span>
                <button
                  class="platform-btn platform-btn-sm"
                  style="margin-left: 8px"
                  @click="toggleAllChangelogs"
                >
                  {{ allChangelogsExpanded ? '全部收起' : '全部展开' }}
                </button>
              </h3>
              <div
                v-for="candidate in finalCandidates"
                :key="candidate.candidate_id"
                class="evo-clog"
                :class="{ best: candidate.candidate_id === finalBestId }"
                :data-candidate="candidate.candidate_id"
              >
                <div
                  class="cl-head"
                  role="button"
                  style="cursor: pointer"
                  @click="toggleChangelog(candidate.candidate_id)"
                >
                  <span class="cl-toggle">{{
                    isChangelogExpanded(candidate.candidate_id) ? '▾' : '▸'
                  }}</span>
                  <span class="cl-id">{{ candidate.candidate_id }}</span>
                  <span
                    v-if="!candidate.parent_id"
                    class="platform-badge platform-b-mut"
                    >seed</span
                  >
                  <span
                    v-if="candidate.is_rollback"
                    class="platform-badge platform-b-violet"
                    >rollback</span
                  >
                  <span
                    v-if="candidate.candidate_id === finalBestId"
                    class="platform-badge platform-b-amber"
                    >BEST</span
                  >
                  <span class="cl-dur">{{
                    formatDuration(candidateDuration(candidate))
                  }}</span>
                </div>
                <div class="cl-summary">{{
                  changelogSummaryText(candidate.candidate_id)
                }}</div>
                <div class="cl-scores">
                  <span
                    >train
                    <b style="color: var(--p-green)">{{
                      pct(candidate.train?.total_score ?? null)
                    }}</b></span
                  >
                  <span
                    >val
                    <b v-if="candidate.validation" style="color: var(--p-amber)">{{
                      pct(candidate.validation.total_score)
                    }}</b
                    ><span v-else class="platform-dim">skipped</span></span
                  >
                  <span class="platform-dim" style="margin-left: auto"
                    >{{ formatTokens(candidateTokens(candidate)) }} tok</span
                  >
                </div>
                <div v-if="isChangelogExpanded(candidate.candidate_id)" class="cl-body">
                  <div
                    v-if="changelogLoading.has(candidate.candidate_id)"
                    class="platform-mut"
                    style="font-size: 12.5px; padding: 8px 0"
                  >
                    <span class="evo-spinner" style="margin-right: 8px" />正在加载
                    changelog…
                  </div>
                  <div
                    v-else-if="changelogErrors[candidate.candidate_id]"
                    class="platform-hint"
                  >
                    ✕ {{ changelogErrors[candidate.candidate_id] }}
                    <a
                      style="color: var(--p-cyan); cursor: pointer"
                      @click="ensureChangelog(candidate.candidate_id)"
                      >重试</a
                    >
                  </div>
                  <template v-else-if="changelogs[candidate.candidate_id]?.groups">
                    <div
                      v-for="(group, gi) in changelogs[candidate.candidate_id]!.groups"
                      :key="gi"
                      style="margin-bottom: 8px"
                    >
                      <div v-if="group.heading" class="cl-heading">{{
                        group.heading
                      }}</div>
                      <div
                        v-for="(section, si) in group.sections"
                        :key="si"
                        class="cl-section"
                      >
                        <b v-if="section.title">{{ section.title }}</b>
                        <p>{{ section.body.trim() }}</p>
                      </div>
                    </div>
                  </template>
                  <pre v-else class="platform-yaml evo-clog-raw">{{
                    changelogs[candidate.candidate_id]?.markdown ?? ''
                  }}</pre>
                </div>
              </div>
            </div>

            <div style="display: flex; flex-direction: column; gap: 16px">
              <div class="platform-card evo-panel">
                <h3>评分趋势 <span class="right">total_score per candidate</span></h3>
                <TrendChart :points="finalTrendPoints" :best="finalBestValidation" />
              </div>
              <div class="platform-card evo-panel">
                <h3>每轮资源消耗 <span class="right">tokens / duration</span></h3>
                <BarChart
                  :values="finalTokenBars"
                  :formatter="(v: number) => `${(v / 1000).toFixed(0)}k`"
                  color="var(--p-cyan)"
                />
                <BarChart
                  :values="finalDurationBars"
                  :formatter="(v: number) => `${v.toFixed(1)}m`"
                  color="var(--p-violet)"
                />
                <div class="evo-legend-line">
                  <span
                    ><i style="background: var(--p-cyan)" />tokens（train +
                    validation）</span
                  >
                  <span><i style="background: var(--p-violet)" />单轮耗时（分钟）</span>
                </div>
              </div>
              <div class="platform-card evo-panel">
                <h3
                  >Train Case 通过矩阵
                  <span class="right">行 = case · 列 = 候选</span></h3
                >
                <CaseHeatmap :case-ids="heatmapCaseIds" :columns="heatmapColumns" />
              </div>
            </div>
          </div>

          <div class="platform-card evo-panel" style="margin-top: 16px">
            <h3
              >候选对比（evolution status）
              <span class="right">总分 | 通过/总数</span></h3
            >
            <table class="evo-cmp">
              <thead>
                <tr>
                  <th>候选</th><th>变更摘要</th><th>训练集</th><th>验证集</th>
                  <th>Tokens</th><th>耗时</th><th>状态</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="candidate in finalCandidates"
                  :key="candidate.candidate_id"
                  :class="{ bestrow: candidate.candidate_id === finalBestId }"
                >
                  <td>
                    {{ candidate.candidate_id }}
                    <span
                      v-if="candidate.is_rollback"
                      class="platform-badge platform-b-violet"
                      >rollback</span
                    >
                  </td>
                  <td
                    class="cmp-change"
                    :title="changelogSummaryCell(candidate.candidate_id)"
                    >{{ changelogSummaryCell(candidate.candidate_id) }}</td
                  >
                  <td>{{ roleResultText(candidate.train) }}</td>
                  <td>{{
                    candidate.validation ? roleResultText(candidate.validation) : '—'
                  }}</td>
                  <td>{{
                    candidateTokens(candidate) > 0
                      ? candidateTokens(candidate).toLocaleString()
                      : '—'
                  }}</td>
                  <td>{{ formatDuration(candidateDuration(candidate)) }}</td>
                  <td>
                    <span
                      class="platform-badge"
                      :class="candidateStatusMeta(candidate).cls"
                    >
                      {{ candidateStatusMeta(candidate).label }}
                    </span>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </template>
      </div>
    </div>

    <Teleport to="body">
      <div
        v-if="stopConfirm"
        class="platform-modal-mask"
        @click.self="stopConfirm = false"
      >
        <div class="platform-modal" role="dialog" aria-label="停止优化任务">
          <h3>停止优化任务</h3>
          <p
            >将请求停止当前 Run（{{
              run?.run_id
            }}）。已经完成的候选评估会保留，进行中的迭代会中断。</p
          >
          <div class="platform-modal-actions">
            <button
              class="platform-btn platform-btn-sm"
              :disabled="stopBusy"
              @click="stopConfirm = false"
              >取消</button
            >
            <button
              class="platform-btn platform-btn-sm platform-btn-dngr"
              :disabled="stopBusy"
              @click="confirmStop"
            >
              {{ stopBusy ? '停止中…' : '确认停止' }}
            </button>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
.evo-mon-head {
  display: flex;
  align-items: center;
  gap: 16px;
  margin-bottom: 18px;
  flex-wrap: wrap;
}
.evo-mon-head h1 {
  font-size: 19px;
  font-weight: 700;
  margin: 0;
}
.evo-run-meta {
  font-size: 11.5px;
  color: var(--p-mut);
  display: flex;
  gap: 16px;
}
.platform-stat-sub {
  font-size: 11px;
  color: var(--p-dim);
  margin-top: 3px;
  font-family: var(--p-mono);
}
.evo-mon-grid {
  display: grid;
  grid-template-columns: 390px 1fr;
  gap: 16px;
  align-items: start;
}
.evo-panel {
  padding: 16px 18px;
}
.evo-panel h3 {
  font-size: 13px;
  font-weight: 600;
  margin: 0 0 12px;
  display: flex;
  align-items: center;
  gap: 8px;
}
.evo-panel h3 .right {
  margin-left: auto;
  font-weight: 400;
  font-size: 11px;
  color: var(--p-dim);
  font-family: var(--p-mono);
}
.evo-case-line {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px 2px;
  border-bottom: 1px dashed rgba(148, 163, 255, 0.07);
  font-family: var(--p-mono);
  font-size: 11.5px;
}
.evo-case-line:last-child {
  border-bottom: none;
}
.evo-case-line .cname {
  color: var(--p-mut);
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.evo-case-line .ctok {
  color: var(--p-dim);
  font-size: 10.5px;
}
.evo-spinner {
  width: 11px;
  height: 11px;
  border: 2px solid rgba(34, 211, 238, 0.25);
  border-top-color: var(--p-cyan);
  border-radius: 50%;
  animation: evo-spin 0.8s linear infinite;
  flex: none;
  display: inline-block;
}
@keyframes evo-spin {
  to {
    transform: rotate(360deg);
  }
}
.evo-pbar {
  height: 5px;
  border-radius: 99px;
  background: rgba(148, 163, 255, 0.12);
  overflow: hidden;
  margin: 10px 0 4px;
}
.evo-pbar i {
  display: block;
  height: 100%;
  background: var(--p-grad);
  border-radius: 99px;
  transition: width 0.4s ease;
}
.evo-log-box {
  font-family: var(--p-mono);
  font-size: 11px;
  line-height: 1.8;
  background: rgba(4, 6, 13, 0.55);
  border: 1px solid var(--p-border);
  border-radius: 10px;
  padding: 12px 14px;
  height: 238px;
  overflow-y: auto;
}
.evo-log-box .lt {
  color: var(--p-dim);
  margin-right: 8px;
}
.evo-log-box .lv-info {
  color: var(--p-cyan);
  margin-right: 8px;
}
.evo-log-box .lv-warn {
  color: var(--p-amber);
  margin-right: 8px;
}
.evo-log-box .lv-err {
  color: var(--p-red);
  margin-right: 8px;
}
.evo-log-box .lm {
  color: #9fb0d0;
}
.evo-clog {
  border: 1px solid var(--p-border);
  border-radius: 12px;
  padding: 12px 14px;
  margin-bottom: 12px;
  background: rgba(4, 6, 13, 0.35);
}
.evo-clog.best {
  border-color: rgba(251, 191, 36, 0.4);
}
.evo-clog .cl-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 7px;
}
.evo-clog .cl-id {
  font-family: var(--p-mono);
  font-size: 12px;
  font-weight: 600;
}
.evo-clog .cl-dur {
  margin-left: auto;
  font-family: var(--p-mono);
  font-size: 10.5px;
  color: var(--p-dim);
}
.evo-clog .cl-toggle {
  color: var(--p-dim);
  font-size: 11px;
  width: 12px;
  flex: none;
  user-select: none;
}
.evo-clog .cl-summary {
  font-size: 12px;
  color: var(--p-mut);
  margin-bottom: 8px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.evo-clog .cl-body {
  border-top: 1px dashed var(--p-border);
  margin-top: 9px;
  padding-top: 9px;
}
.evo-clog .cl-heading {
  font-size: 12.5px;
  font-weight: 600;
  margin-bottom: 4px;
}
.evo-clog .cl-section {
  font-size: 12px;
  margin-bottom: 6px;
}
.evo-clog .cl-section b {
  display: block;
  color: var(--p-mut);
  font-size: 11.5px;
}
.evo-clog .cl-section p {
  margin: 2px 0 0;
  color: var(--p-txt);
  white-space: pre-wrap;
  word-break: break-word;
}
.evo-clog .cl-scores {
  display: flex;
  gap: 14px;
  margin-top: 9px;
  font-family: var(--p-mono);
  font-size: 11.5px;
  color: var(--p-mut);
}
.evo-clog-raw {
  background: rgba(4, 6, 13, 0.55);
  border: 1px solid var(--p-border);
  border-radius: 10px;
  max-height: 220px;
  overflow: auto;
}
.evo-cmp {
  width: 100%;
  border-collapse: collapse;
  font-size: 12.5px;
}
.evo-cmp th {
  text-align: left;
  font-size: 11px;
  color: var(--p-dim);
  font-weight: 500;
  padding: 7px 10px;
  border-bottom: 1px solid var(--p-border);
  font-family: var(--p-mono);
}
.evo-cmp td {
  padding: 8px 10px;
  border-bottom: 1px solid rgba(148, 163, 255, 0.06);
  font-family: var(--p-mono);
  font-size: 11.5px;
}
.evo-cmp tr.bestrow td {
  background: rgba(251, 191, 36, 0.045);
}
.evo-cmp .cmp-change {
  max-width: 230px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.evo-legend-line {
  display: flex;
  gap: 16px;
  font-size: 11.5px;
  color: var(--p-mut);
  font-family: var(--p-mono);
  margin-top: 6px;
}
.evo-legend-line i {
  display: inline-block;
  width: 14px;
  height: 3px;
  border-radius: 2px;
  margin-right: 5px;
  vertical-align: middle;
}
@media (max-width: 1200px) {
  .evo-mon-grid {
    grid-template-columns: 1fr !important;
  }
}
</style>
