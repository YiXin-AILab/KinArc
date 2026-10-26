<script setup lang="ts">
/**
 * 评测任务列表页（/platform/evaluation）。
 *
 * 展示评测任务、近期运行及启动入口；点击运行后进入详情页，
 * 由详情页轮询进度并展示报告。
 */

import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import {
  defaultEvaluationClient,
  type EvaluationClient,
  type EvaluationRunView,
  type EvaluationTaskSummary,
} from './evaluationClient'

const props = withDefaults(defineProps<{ client?: EvaluationClient }>(), {
  client: undefined,
})
const client = props.client ?? defaultEvaluationClient()
const router = useRouter()

const tasks = ref<EvaluationTaskSummary[] | null>(null)
const runsByTask = ref<Record<string, EvaluationRunView[]>>({})
const error = ref<string | null>(null)
const startingTaskId = ref<string | null>(null)

const STATUS_META: Record<string, { label: string; cls: string }> = {
  pending: { label: '排队中', cls: 'platform-b-mut' },
  running: { label: '运行中', cls: 'platform-b-cyan' },
  succeeded: { label: '已完成', cls: 'platform-b-green' },
  failed: { label: '失败', cls: 'platform-b-red' },
}

function formatTime(epochSeconds: number): string {
  return new Date(epochSeconds * 1000).toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function formatScore(score: number | null): string {
  return score === null ? '—' : `${(score * 100).toFixed(1)}%`
}

async function load(): Promise<void> {
  error.value = null
  try {
    const list = await client.listTasks()
    tasks.value = list
    const entries = await Promise.all(
      list.map(async (task) => {
        try {
          const detail = await client.taskDetail(task.task_id)
          return [task.task_id, detail.runs] as const
        } catch {
          return [task.task_id, []] as const
        }
      }),
    )
    runsByTask.value = Object.fromEntries(entries)
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : '评测任务加载失败'
  }
}

/**
 * 启动指定任务的评测运行，并进入详情页观察进度。
 */
async function startRun(task: EvaluationTaskSummary): Promise<void> {
  if (startingTaskId.value !== null) return
  startingTaskId.value = task.task_id
  error.value = null
  try {
    const run = await client.startRun(task.task_id)
    await router.push(`/platform/evaluation/runs/${run.run_id}`)
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : '发起评测失败'
  } finally {
    startingTaskId.value = null
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
            <h1>评测</h1>
            <p
              >Agent 评测任务与历史
              Run。发起评测后进入详情页轮询进度并查看报告与对比。</p
            >
          </div>
          <button class="platform-btn" @click="load">刷新</button>
        </div>

        <div v-if="error" class="platform-state">
          <p role="alert">{{ error }}</p>
          <button class="platform-btn" @click="load">重试</button>
        </div>
        <div v-else-if="!tasks" class="platform-state">正在加载评测任务…</div>
        <div v-else-if="tasks.length === 0" class="platform-state">
          <p>暂无评测任务</p>
        </div>
        <div v-else class="eval-task-list">
          <div
            v-for="task in tasks"
            :key="task.task_id"
            class="platform-card eval-task"
          >
            <div class="eval-task-head">
              <span class="eval-task-id">{{ task.task_id }}</span>
              <span v-if="!task.valid" class="platform-badge platform-b-red">
                配置无效
              </span>
              <button
                class="platform-btn platform-btn-pri platform-btn-sm"
                style="margin-left: auto"
                :disabled="!task.valid || startingTaskId !== null"
                @click="startRun(task)"
              >
                {{ startingTaskId === task.task_id ? '发起中…' : '发起评测' }}
              </button>
            </div>
            <div class="eval-task-meta">
              <span
                >Agent <b>{{ task.agent_id || '—' }}</b></span
              >
              <span
                >数据集 <b>{{ task.dataset || '—' }}</b></span
              >
              <span
                >Case 数 <b>{{ task.case_count }}</b></span
              >
              <span
                >历史 Run <b>{{ task.run_count }}</b></span
              >
            </div>
            <p v-if="task.error" class="platform-agent-card-error"
              >✕ {{ task.error }}</p
            >

            <div v-if="(runsByTask[task.task_id] ?? []).length > 0" class="eval-runs">
              <div
                v-for="run in runsByTask[task.task_id]"
                :key="run.run_id"
                class="eval-run-row"
                @click="router.push(`/platform/evaluation/runs/${run.run_id}`)"
              >
                <span class="platform-badge" :class="STATUS_META[run.status]?.cls">
                  <span class="platform-dot" />{{
                    STATUS_META[run.status]?.label ?? run.status
                  }}
                </span>
                <span class="platform-mono eval-run-id">{{ run.run_id }}</span>
                <span class="eval-run-score">{{ formatScore(run.final_score) }}</span>
                <span class="platform-dim">{{ formatTime(run.created_at) }}</span>
                <span
                  v-if="run.status === 'running' || run.status === 'pending'"
                  class="platform-dim"
                >
                  {{ run.completed_cases }}/{{ run.case_count ?? '?' }}
                </span>
              </div>
            </div>
            <p v-else class="platform-dim eval-no-run">尚无 Run 记录</p>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.eval-task-list {
  display: grid;
  gap: 14px;
}
.eval-task {
  padding: 16px 20px;
}
.eval-task-head {
  display: flex;
  align-items: center;
  gap: 10px;
}
.eval-task-id {
  font-size: 15px;
  font-weight: 600;
}
.eval-task-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 18px;
  margin-top: 8px;
  color: var(--p-mut);
  font-size: 12.5px;
}
.eval-runs {
  display: grid;
  gap: 6px;
  margin-top: 12px;
}
.eval-run-row {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 7px 10px;
  border: 1px solid var(--p-border);
  border-radius: 8px;
  cursor: pointer;
  font-size: 12.5px;
  transition: 0.15s;
}
.eval-run-row:hover {
  background: var(--p-panel2);
}
.eval-run-id {
  color: var(--p-mut);
  font-size: 11.5px;
}
.eval-run-score {
  min-width: 52px;
  color: var(--p-amber);
  font-weight: 600;
}
.eval-no-run {
  margin: 10px 0 0;
  font-size: 12.5px;
}
</style>
