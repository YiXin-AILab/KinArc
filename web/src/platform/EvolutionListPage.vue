<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import { defaultEvolutionClient, type EvolutionTaskSummary } from './evolutionClient'

const client = defaultEvolutionClient()
const router = useRouter()

const tasks = ref<EvolutionTaskSummary[] | null>(null)
const error = ref<string | null>(null)

const runningCount = computed(
  () => (tasks.value ?? []).filter((task) => task.active_run_id !== null).length,
)

function statusMeta(task: EvolutionTaskSummary): { label: string; cls: string } {
  if (task.active_run_id !== null) return { label: 'RUNNING', cls: 'platform-b-cyan' }
  if (task.candidate_count > 0) return { label: 'COMPLETED', cls: 'platform-b-green' }
  return { label: '无运行', cls: 'platform-b-mut' }
}

function formatTokens(tokens: number): string {
  if (tokens <= 0) return '—'
  if (tokens >= 1000) return `${(tokens / 1000).toFixed(0)}k`
  return String(tokens)
}

async function load(): Promise<void> {
  error.value = null
  try {
    tasks.value = await client.listTasks()
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : '任务列表加载失败'
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
            <h1>自进化任务</h1>
            <p
              >历史与进行中的优化任务。点击进入详情：每轮改进策略（changelog）、指标趋势、case
              通过矩阵与候选对比。</p
            >
          </div>
          <button
            class="platform-btn platform-btn-pri"
            @click="router.push('/platform/evolution/new')"
            >＋ 新建优化任务</button
          >
        </div>

        <div v-if="error" class="platform-state">
          <p role="alert">{{ error }}</p>
          <button class="platform-btn" @click="load">重试</button>
        </div>
        <div v-else-if="!tasks" class="platform-state">正在加载自进化任务…</div>
        <div v-else-if="tasks.length === 0" class="platform-state">
          <p>暂无自进化任务</p>
          <button
            class="platform-btn platform-btn-pri"
            @click="router.push('/platform/evolution/new')"
            >＋ 新建优化任务</button
          >
        </div>
        <template v-else>
          <div class="platform-stats" style="grid-template-columns: repeat(3, 1fr)">
            <div class="platform-card platform-stat">
              <div class="platform-stat-label">任务总数</div>
              <div class="platform-stat-value">{{ tasks.length }}</div>
            </div>
            <div class="platform-card platform-stat">
              <div class="platform-stat-label">运行中</div>
              <div class="platform-stat-value" style="color: var(--p-cyan)">{{
                runningCount
              }}</div>
            </div>
            <div class="platform-card platform-stat">
              <div class="platform-stat-label">累计 Tokens</div>
              <div class="platform-stat-value">{{
                formatTokens(tasks.reduce((sum, task) => sum + task.total_tokens, 0))
              }}</div>
            </div>
          </div>

          <div class="platform-grid-2">
            <div
              v-for="task in tasks"
              :key="task.task_id"
              class="platform-card evo-task-card"
              @click="router.push(`/platform/evolution/${task.task_id}`)"
            >
              <div class="evo-task-head">
                <span class="evo-task-id">{{ task.task_id }}</span>
                <span
                  class="platform-badge"
                  :class="statusMeta(task).cls"
                  style="margin-left: auto"
                >
                  <span class="platform-dot" />{{ statusMeta(task).label }}
                </span>
              </div>
              <p class="evo-task-obj">{{ task.objective }}</p>
              <p v-if="task.error" class="platform-agent-card-error"
                >✕ {{ task.error }}</p
              >
              <div class="evo-task-metrics">
                <span
                  >最佳验证<b style="color: var(--p-amber)">{{
                    task.best_validation_score !== null
                      ? (task.best_validation_score * 100).toFixed(1) + '%'
                      : '—'
                  }}</b></span
                >
                <span
                  >候选<b
                    >{{ task.candidate_count }} / {{ task.max_iterations }}</b
                  ></span
                >
                <span
                  >Tokens<b>{{ formatTokens(task.total_tokens) }}</b></span
                >
              </div>
              <div class="evo-task-foot">
                <span>{{ task.initial_target }}</span>
                <span v-if="task.best_candidate_id"
                  >best: {{ task.best_candidate_id }}</span
                >
              </div>
            </div>

            <div
              class="platform-card platform-newcard"
              @click="router.push('/platform/evolution/new')"
            >
              <div>
                <div class="platform-newcard-plus">＋</div>
                <div style="font-size: 13.5px"
                  >新建优化任务<br /><span class="platform-dim" style="font-size: 12px"
                    >从模板克隆 · 配置迭代策略</span
                  ></div
                >
              </div>
            </div>
          </div>
        </template>
      </div>
    </div>
  </div>
</template>

<style scoped>
.evo-task-card {
  padding: 18px 20px;
  cursor: pointer;
  transition: 0.18s;
}
.evo-task-card:hover {
  border-color: var(--p-border2);
  background: var(--p-panel2);
}
.evo-task-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
}
.evo-task-id {
  font-family: var(--p-mono);
  font-size: 13.5px;
  font-weight: 600;
}
.evo-task-obj {
  font-size: 12px;
  color: var(--p-mut);
  margin: 0 0 12px;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.evo-task-metrics {
  display: flex;
  gap: 22px;
  font-family: var(--p-mono);
  font-size: 11px;
  color: var(--p-mut);
}
.evo-task-metrics b {
  color: var(--p-txt);
  font-size: 14px;
  display: block;
  margin-top: 1px;
}
.evo-task-foot {
  display: flex;
  justify-content: space-between;
  gap: 10px;
  margin-top: 12px;
  font-size: 11px;
  color: var(--p-dim);
  font-family: var(--p-mono);
}
</style>
