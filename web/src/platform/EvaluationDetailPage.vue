<script setup lang="ts">
/**
 * 评测运行详情页（/platform/evaluation/runs/:runId）。
 *
 * 每 2.5 秒轮询运行快照，进入终态后展示评测报告：总分、各维度均分、
 * 可展开的用例规则与维度评分，以及高亮的失败用例。
 * 支持选择基线运行并比较评分差异。
 */

import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import {
  defaultEvaluationClient,
  type ComparisonReport,
  type EvalReport,
  type EvaluationClient,
  type EvaluationRunView,
} from './evaluationClient'

const props = withDefaults(defineProps<{ client?: EvaluationClient }>(), {
  client: undefined,
})
const client = props.client ?? defaultEvaluationClient()
const route = useRoute()

const POLL_INTERVAL_MS = 2500

const run = ref<EvaluationRunView | null>(null)
const report = ref<EvalReport | null>(null)
const error = ref<string | null>(null)
const reportError = ref<string | null>(null)
const expandedCases = ref<Set<string>>(new Set())
const siblingRuns = ref<EvaluationRunView[]>([])
const baselineId = ref<string>('')
const comparison = ref<ComparisonReport | null>(null)
const compareError = ref<string | null>(null)
const comparing = ref(false)

let pollTimer: ReturnType<typeof setTimeout> | undefined
const pollController = new AbortController()

const runId = computed(() =>
  typeof route.params.runId === 'string' ? route.params.runId : '',
)

const isTerminal = computed(
  () => run.value?.status === 'succeeded' || run.value?.status === 'failed',
)

const baselineOptions = computed(() =>
  siblingRuns.value.filter(
    (item) => item.status === 'succeeded' && item.run_id !== runId.value,
  ),
)

const STATUS_META: Record<string, { label: string; cls: string }> = {
  pending: { label: '排队中', cls: 'platform-b-mut' },
  running: { label: '运行中', cls: 'platform-b-cyan' },
  succeeded: { label: '已完成', cls: 'platform-b-green' },
  failed: { label: '失败', cls: 'platform-b-red' },
}

const DELTA_META = {
  improved: { label: '改善', cls: 'platform-b-green' },
  regressed: { label: '回归', cls: 'platform-b-red' },
  unchanged: { label: '持平', cls: 'platform-b-mut' },
} as const

function formatScore(score: number | null | undefined): string {
  return score === null || score === undefined ? '—' : `${(score * 100).toFixed(1)}%`
}

function formatDelta(delta: number): string {
  const percent = (delta * 100).toFixed(1)
  return delta > 0 ? `+${percent}%` : `${percent}%`
}

function formatTime(epochSeconds: number | null): string {
  if (epochSeconds === null) return '—'
  return new Date(epochSeconds * 1000).toLocaleString('zh-CN')
}

function toggleCase(caseId: string): void {
  const next = new Set(expandedCases.value)
  if (next.has(caseId)) {
    next.delete(caseId)
  } else {
    next.add(caseId)
  }
  expandedCases.value = next
}

/**
 * 轮询运行直至终态，再加载报告及同一任务下可选作基线的其他运行。
 */
async function pollRun(): Promise<void> {
  try {
    const snapshot = await client.getRun(runId.value, pollController.signal)
    run.value = snapshot
    if (snapshot.status === 'pending' || snapshot.status === 'running') {
      pollTimer = setTimeout(() => void pollRun(), POLL_INTERVAL_MS)
      return
    }
    if (snapshot.status === 'succeeded') {
      await Promise.all([loadReport(), loadSiblingRuns(snapshot.task_id)])
    }
  } catch (caught) {
    if (pollController.signal.aborted) return
    error.value = caught instanceof Error ? caught.message : 'Run 状态加载失败'
  }
}

async function loadReport(): Promise<void> {
  reportError.value = null
  try {
    report.value = await client.getReport(runId.value, pollController.signal)
  } catch (caught) {
    if (pollController.signal.aborted) return
    reportError.value = caught instanceof Error ? caught.message : '报告加载失败'
  }
}

async function loadSiblingRuns(taskId: string): Promise<void> {
  try {
    const detail = await client.taskDetail(taskId, pollController.signal)
    siblingRuns.value = detail.runs
  } catch {
    siblingRuns.value = []
  }
}

async function runCompare(): Promise<void> {
  if (!baselineId.value || comparing.value) return
  comparing.value = true
  compareError.value = null
  comparison.value = null
  try {
    comparison.value = await client.compare(runId.value, baselineId.value)
  } catch (caught) {
    compareError.value = caught instanceof Error ? caught.message : '对比失败'
  } finally {
    comparing.value = false
  }
}

watch(baselineId, () => {
  comparison.value = null
  compareError.value = null
})

onMounted(() => void pollRun())
onBeforeUnmount(() => {
  clearTimeout(pollTimer)
  pollController.abort()
})
</script>

<template>
  <div class="platform-root">
    <div class="platform-page">
      <div class="platform-page-inner">
        <div class="platform-page-head">
          <div>
            <h1>评测 Run 详情</h1>
            <p>
              <span class="platform-mono">{{ runId }}</span>
              <RouterLink to="/platform/evaluation" class="run-back">
                ← 返回评测列表
              </RouterLink>
            </p>
          </div>
          <span v-if="run" class="platform-badge" :class="STATUS_META[run.status]?.cls">
            <span class="platform-dot" />{{
              STATUS_META[run.status]?.label ?? run.status
            }}
          </span>
        </div>

        <div v-if="error" class="platform-state">
          <p role="alert">{{ error }}</p>
        </div>
        <template v-else-if="run">
          <div class="platform-stats" style="grid-template-columns: repeat(4, 1fr)">
            <div class="platform-card platform-stat">
              <div class="platform-stat-label">最终得分</div>
              <div class="platform-stat-value" style="color: var(--p-amber)">
                {{ formatScore(run.final_score) }}
              </div>
            </div>
            <div class="platform-card platform-stat">
              <div class="platform-stat-label">进度</div>
              <div class="platform-stat-value">
                {{ run.completed_cases }}/{{ run.case_count ?? '?' }}
              </div>
            </div>
            <div class="platform-card platform-stat">
              <div class="platform-stat-label">创建时间</div>
              <div class="platform-stat-value run-time">{{
                formatTime(run.created_at)
              }}</div>
            </div>
            <div class="platform-card platform-stat">
              <div class="platform-stat-label">结束时间</div>
              <div class="platform-stat-value run-time">{{
                formatTime(run.finished_at)
              }}</div>
            </div>
          </div>
          <div
            v-if="run.status === 'running' || run.status === 'pending'"
            class="platform-notice platform-notice-cyan"
          >
            Run 进行中，每 {{ POLL_INTERVAL_MS / 1000 }} 秒自动刷新…
          </div>
          <div v-if="run.error" class="platform-notice" role="alert"
            >✕ {{ run.error }}</div
          >
        </template>
        <div v-else class="platform-state">正在加载 Run…</div>

        <template v-if="report">
          <h2 class="run-sec">维度均分</h2>
          <div class="platform-stats run-dims">
            <div
              v-for="(average, dimension) in report.dim_averages"
              :key="dimension"
              class="platform-card platform-stat"
            >
              <div class="platform-stat-label">{{ dimension }}</div>
              <div class="platform-stat-value">{{ formatScore(average) }}</div>
            </div>
          </div>

          <h2 class="run-sec">
            Case 明细
            <span class="platform-dim run-sec-sub">
              共 {{ report.case_count }} 条，失败 {{ report.failed_count }} 条
            </span>
          </h2>
          <div class="run-cases">
            <div
              v-for="item in report.cases"
              :key="item.case_id"
              class="platform-card run-case"
              :class="{ 'run-case-failed': item.failed }"
            >
              <div class="run-case-head" @click="toggleCase(item.case_id)">
                <span class="platform-mono run-case-id">{{ item.case_id }}</span>
                <span v-if="item.failed" class="platform-badge platform-b-red"
                  >失败</span
                >
                <span class="run-case-score">{{ formatScore(item.final_score) }}</span>
                <span class="platform-dim">{{
                  expandedCases.has(item.case_id) ? '▲' : '▼'
                }}</span>
              </div>
              <div v-if="expandedCases.has(item.case_id)" class="run-case-body">
                <div v-if="item.rules.length > 0" class="run-case-block">
                  <div class="platform-side-label">规则结果</div>
                  <div v-for="rule in item.rules" :key="rule.rule" class="run-rule">
                    <span :class="rule.passed ? 'run-pass' : 'run-fail'">
                      {{ rule.passed ? '✓' : '✕' }}
                    </span>
                    <span class="platform-mono">{{ rule.rule }}</span>
                    <span class="platform-dim">{{ rule.detail }}</span>
                  </div>
                </div>
                <div v-if="item.dims.length > 0" class="run-case-block">
                  <div class="platform-side-label">维度评分</div>
                  <div v-for="dim in item.dims" :key="dim.dimension" class="run-dim">
                    <span class="run-dim-name">{{ dim.dimension }}</span>
                    <span class="run-dim-score">{{ formatScore(dim.score) }}</span>
                    <span class="platform-mut run-dim-rationale">
                      {{ dim.error ? `评分失败：${dim.error}` : dim.rationale }}
                    </span>
                  </div>
                </div>
              </div>
            </div>
          </div>

          <h2 class="run-sec">Baseline 对比</h2>
          <div class="platform-card run-compare">
            <div class="run-compare-bar">
              <select
                v-model="baselineId"
                class="run-select"
                aria-label="选择 baseline Run"
              >
                <option value="">选择 baseline Run…</option>
                <option
                  v-for="option in baselineOptions"
                  :key="option.run_id"
                  :value="option.run_id"
                >
                  {{ option.run_id }}（{{ formatScore(option.final_score) }}）
                </option>
              </select>
              <button
                class="platform-btn platform-btn-pri platform-btn-sm"
                :disabled="!baselineId || comparing"
                @click="runCompare"
              >
                {{ comparing ? '对比中…' : '对比' }}
              </button>
            </div>
            <p
              v-if="baselineOptions.length === 0"
              class="platform-dim run-compare-hint"
            >
              该任务暂无其他已完成的 Run 可作为 baseline。
            </p>
            <p v-if="compareError" class="platform-agent-card-error" role="alert">
              ✕ {{ compareError }}
            </p>
            <template v-if="comparison">
              <div class="run-compare-summary">
                <span class="platform-badge platform-b-green">
                  改善 {{ comparison.improved_count }}
                </span>
                <span class="platform-badge platform-b-red">
                  回归 {{ comparison.regressed_count }}
                </span>
                <span class="platform-badge platform-b-mut">
                  持平 {{ comparison.unchanged_count }}
                </span>
                <span class="run-compare-delta">
                  总分 {{ formatDelta(comparison.final_score_delta) }}
                </span>
              </div>
              <div
                v-if="Object.keys(comparison.dim_deltas).length > 0"
                class="run-compare-dims"
              >
                <span
                  v-for="(delta, dimension) in comparison.dim_deltas"
                  :key="dimension"
                  class="platform-dim"
                >
                  {{ dimension }}
                  <b :class="delta >= 0 ? 'run-pass' : 'run-fail'">{{
                    formatDelta(delta)
                  }}</b>
                </span>
              </div>
              <div class="run-delta-list">
                <div
                  v-for="delta in comparison.case_deltas"
                  :key="delta.case_id"
                  class="run-delta-row"
                >
                  <span class="platform-badge" :class="DELTA_META[delta.status].cls">
                    {{ DELTA_META[delta.status].label }}
                  </span>
                  <span class="platform-mono run-case-id">{{ delta.case_id }}</span>
                  <span class="platform-dim">
                    {{ formatScore(delta.baseline_score) }} →
                    {{ formatScore(delta.current_score) }}
                  </span>
                  <b :class="delta.delta >= 0 ? 'run-pass' : 'run-fail'">
                    {{ formatDelta(delta.delta) }}
                  </b>
                </div>
              </div>
            </template>
          </div>
        </template>
        <div v-else-if="reportError" class="platform-state">
          <p role="alert">{{ reportError }}</p>
        </div>
        <div
          v-else-if="isTerminal && run?.status === 'succeeded'"
          class="platform-state"
        >
          正在加载报告…
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.run-back {
  margin-left: 12px;
  color: var(--p-cyan);
  font-size: 12.5px;
  text-decoration: none;
}
.run-time {
  font-size: 14px;
}
.run-sec {
  margin: 22px 0 12px;
  font-size: 15px;
}
.run-sec-sub {
  margin-left: 10px;
  font-size: 12px;
  font-weight: 400;
}
.run-dims {
  grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
}
.run-cases {
  display: grid;
  gap: 8px;
}
.run-case {
  padding: 10px 14px;
}
.run-case-failed {
  border-color: rgba(248, 113, 113, 0.45);
}
.run-case-head {
  display: flex;
  align-items: center;
  gap: 10px;
  cursor: pointer;
}
.run-case-id {
  font-size: 12px;
}
.run-case-score {
  margin-left: auto;
  color: var(--p-amber);
  font-weight: 600;
}
.run-case-body {
  display: grid;
  gap: 12px;
  margin-top: 10px;
  border-top: 1px solid var(--p-border);
  padding-top: 10px;
}
.run-case-block {
  display: grid;
  gap: 6px;
}
.run-rule,
.run-dim {
  display: flex;
  align-items: baseline;
  gap: 8px;
  font-size: 12.5px;
}
.run-pass {
  color: var(--p-green);
}
.run-fail {
  color: var(--p-red);
}
.run-dim-name {
  min-width: 110px;
  color: var(--p-mut);
}
.run-dim-score {
  color: var(--p-amber);
  font-weight: 600;
}
.run-dim-rationale {
  flex: 1;
  font-size: 12px;
  line-height: 1.6;
}
.run-compare {
  padding: 14px 18px;
}
.run-compare-bar {
  display: flex;
  gap: 10px;
}
.run-select {
  flex: 1;
  max-width: 460px;
  padding: 8px 10px;
  border: 1px solid var(--p-border);
  border-radius: 8px;
  background: var(--p-bg1);
  color: var(--p-txt);
  font: inherit;
  font-size: 12.5px;
}
.run-compare-hint {
  margin: 10px 0 0;
  font-size: 12px;
}
.run-compare-summary {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 14px;
}
.run-compare-delta {
  margin-left: auto;
  color: var(--p-amber);
  font-weight: 600;
}
.run-compare-dims {
  display: flex;
  flex-wrap: wrap;
  gap: 14px;
  margin-top: 10px;
  font-size: 12.5px;
}
.run-delta-list {
  display: grid;
  gap: 6px;
  margin-top: 12px;
}
.run-delta-row {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 12.5px;
}
</style>
