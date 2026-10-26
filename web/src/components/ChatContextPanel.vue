<script setup lang="ts">
/**
 * 右侧上下文面板：展示运行状态、执行轨迹和 Agent 元数据。
 *
 * 运行数据来自对话控制器的实时状态；Agent 元数据从平台目录获取，
 * 获取失败时使用 Runtime 中的 Agent 定义，不中断对话。
 */

import { computed, onMounted, ref, watch } from 'vue'

import type { AgentSummary } from '../api/platformClient'
import type { AgentDefinition } from '../config'
import type { SessionSnapshot, SessionStatus, StreamTimeline } from '../domain/types'
import { defaultPlatformClient } from '../platform/client'

const props = defineProps<{
  snapshot: SessionSnapshot | null
  timeline: StreamTimeline
  agent: AgentDefinition
  /** 可注入的 Agent 元数据来源；默认走平台 API，失败时静默降级。 */
  loadAgentMeta?: () => Promise<AgentSummary[]>
}>()

interface CtxStep {
  key: string
  label: string
  state: 'run' | 'done' | 'fail'
}

const STATUS_META: Record<SessionStatus, { label: string; cls: string }> = {
  idle: { label: '空闲', cls: 'ctx-b-mut' },
  running: { label: '运行中', cls: 'ctx-b-cyan' },
  pending: { label: '待确认', cls: 'ctx-b-amber' },
  succeeded: { label: '已完成', cls: 'ctx-b-green' },
  failed: { label: '失败', cls: 'ctx-b-red' },
  canceled: { label: '已取消', cls: 'ctx-b-mut' },
}

const agentMeta = ref<AgentSummary | null>(null)

/** Runtime agent id 与平台 agent_id 的命名可能只差 -/_ 分隔，逐个候选匹配。 */
function matchAgentMeta(
  list: AgentSummary[],
  agent: AgentDefinition,
): AgentSummary | null {
  const normalizedId = agent.id.replace(/-/g, '_')
  return (
    list.find(
      (item) =>
        item.agent_id === agent.id ||
        item.agent_id === normalizedId ||
        item.agent_name === agent.name,
    ) ?? null
  )
}

async function loadMeta(): Promise<void> {
  const loader = props.loadAgentMeta ?? (() => defaultPlatformClient().listAgents())
  try {
    agentMeta.value = matchAgentMeta(await loader(), props.agent)
  } catch {
    agentMeta.value = null
  }
}

onMounted(() => void loadMeta())
watch(
  () => props.agent.id,
  () => void loadMeta(),
)

const statusMeta = computed(() => STATUS_META[props.snapshot?.status ?? 'idle'])

const sessionLabel = computed(() => {
  const id = props.snapshot?.session_id
  return id ? `${id.slice(0, 8)}…` : '—'
})

const tokensLabel = computed(() => {
  const total = props.snapshot?.usage?.total_tokens
  return total === undefined || total === null ? '—' : total.toLocaleString()
})

function formatElapsed(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)}ms`
  const seconds = ms / 1000
  if (seconds < 60) return `${seconds.toFixed(1)}s`
  const minutes = Math.floor(seconds / 60)
  return `${minutes}m ${String(Math.round(seconds % 60)).padStart(2, '0')}s`
}

const elapsedLabel = computed(() => {
  const elapsed = props.snapshot?.timing?.elapsed_ms
  return elapsed === undefined || elapsed === null ? '—' : formatElapsed(elapsed)
})

/** 把 timeline 投影成最近 8 条执行步骤；status 类标记不进入轨迹。 */
const steps = computed<CtxStep[]>(() => {
  const derived: CtxStep[] = []
  for (const item of props.timeline.items) {
    if (item.kind === 'message-boundary') {
      derived.push({ key: item.key, label: '接收输入', state: 'done' })
    } else if (item.kind === 'block') {
      derived.push({
        key: item.key,
        label: item.blockType === 'thinking' ? '思考' : 'LLM 生成',
        state: item.completed ? 'done' : 'run',
      })
    } else if (item.kind === 'tool') {
      derived.push({
        key: item.key,
        label: item.name,
        state:
          item.status === 'running'
            ? 'run'
            : item.status === 'failed'
              ? 'fail'
              : 'done',
      })
    } else if (item.kind === 'compaction') {
      derived.push({
        key: item.key,
        label: '上下文压缩',
        state:
          item.status === 'running'
            ? 'run'
            : item.status === 'failed'
              ? 'fail'
              : 'done',
      })
    }
  }
  return derived.slice(-8)
})

const agentInitial = computed(() => (props.agent.name.trim()[0] ?? 'A').toUpperCase())
</script>

<template>
  <aside class="chat-ctx" aria-label="运行状态面板">
    <section class="ctx-sec">
      <h4>运行状态</h4>
      <div class="ctx-card" data-testid="ctx-run-card">
        <div class="ctx-kv"
          ><span>session</span><span>{{ sessionLabel }}</span></div
        >
        <div class="ctx-kv"
          ><span>agent</span><span>{{ agent.name }}</span></div
        >
        <div class="ctx-kv">
          <span>status</span>
          <span class="ctx-badge" :class="statusMeta.cls" data-testid="ctx-status">
            <span class="ctx-dot" />{{ statusMeta.label }}
          </span>
        </div>
        <div class="ctx-kv"
          ><span>tokens</span><span>{{ tokensLabel }}</span></div
        >
        <div class="ctx-kv"
          ><span>耗时</span><span>{{ elapsedLabel }}</span></div
        >
      </div>
    </section>

    <section class="ctx-sec">
      <h4>执行轨迹</h4>
      <div class="ctx-card ctx-steps" data-testid="ctx-steps">
        <div v-for="step in steps" :key="step.key" class="ctx-step" :class="step.state">
          <span class="ctx-step-label">{{ step.label }}</span>
          <span v-if="step.state === 'done'" class="ctx-step-mark done">✓</span>
          <span v-else-if="step.state === 'fail'" class="ctx-step-mark fail">✗</span>
        </div>
        <div v-if="steps.length === 0" class="ctx-empty">等待运行事件…</div>
      </div>
    </section>

    <section class="ctx-sec">
      <h4>AGENT</h4>
      <div class="ctx-card" data-testid="ctx-agent-card">
        <div class="ctx-agent-head">
          <span class="ctx-agent-mark">{{ agentInitial }}</span>
          <b>{{ agent.name }}</b>
        </div>
        <template v-if="agentMeta">
          <div class="ctx-kv"
            ><span>model</span><span>{{ agentMeta.model ?? '—' }}</span></div
          >
          <div class="ctx-kv"
            ><span>tools</span><span>{{ agentMeta.tool_count }} 启用</span></div
          >
          <div class="ctx-kv"
            ><span>skills</span><span>{{ agentMeta.skill_count }} 挂载</span></div
          >
        </template>
        <p v-else-if="agent.description" class="ctx-agent-desc">{{
          agent.description
        }}</p>
      </div>
    </section>
  </aside>
</template>

<style scoped>
.chat-ctx {
  width: 280px;
  flex: none;
  border-left: 1px solid var(--line);
  background: rgba(6, 9, 18, 0.4);
  overflow-y: auto;
  padding: 16px 14px;
  scrollbar-width: thin;
  scrollbar-color: rgba(148, 163, 255, 0.25) transparent;
}
.ctx-sec {
  margin-bottom: 18px;
}
.ctx-sec h4 {
  font-size: 11px;
  color: var(--dim);
  font-family: var(--mono);
  margin: 0 0 8px;
  letter-spacing: 0.5px;
}
.ctx-card {
  border: 1px solid var(--line);
  border-radius: 11px;
  background: var(--panel);
  padding: 10px 12px;
  font-size: 12px;
}
.ctx-kv {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 10px;
  font-size: 11.5px;
  padding: 3px 0;
  font-family: var(--mono);
}
.ctx-kv > span:first-child {
  color: var(--dim);
  flex: none;
}
.ctx-kv > span:last-child {
  color: var(--ink);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ctx-badge {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 10.5px;
  padding: 2px 8px;
  border-radius: 99px;
  border: 1px solid;
  font-weight: 500;
  white-space: nowrap;
}
.ctx-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentColor;
  box-shadow: 0 0 7px currentColor;
}
.ctx-b-cyan {
  color: var(--cyan);
  border-color: rgba(34, 211, 238, 0.3);
  background: rgba(34, 211, 238, 0.08);
}
.ctx-b-green {
  color: var(--success);
  border-color: rgba(52, 211, 153, 0.3);
  background: rgba(52, 211, 153, 0.08);
}
.ctx-b-amber {
  color: var(--amber);
  border-color: rgba(251, 191, 36, 0.3);
  background: rgba(251, 191, 36, 0.08);
}
.ctx-b-red {
  color: var(--danger);
  border-color: rgba(248, 113, 113, 0.3);
  background: rgba(248, 113, 113, 0.08);
}
.ctx-b-mut {
  color: var(--muted);
  border-color: var(--line);
  background: var(--panel);
}
.ctx-steps {
  display: flex;
  flex-direction: column;
}
.ctx-step {
  display: flex;
  align-items: baseline;
  gap: 9px;
  font-size: 11.5px;
  color: var(--muted);
  position: relative;
  padding-bottom: 12px;
}
.ctx-step:last-child {
  padding-bottom: 0;
}
.ctx-step::before {
  content: '';
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--dim);
  flex: none;
  align-self: center;
}
.ctx-step.done::before {
  background: var(--success);
  box-shadow: 0 0 6px rgba(52, 211, 153, 0.6);
}
.ctx-step.run::before {
  background: var(--cyan);
  box-shadow: 0 0 8px rgba(34, 211, 238, 0.8);
  animation: ctxPulse 1.1s infinite;
}
.ctx-step.fail::before {
  background: var(--danger);
  box-shadow: 0 0 6px rgba(248, 113, 113, 0.6);
}
.ctx-step:not(:last-child)::after {
  content: '';
  position: absolute;
  left: 3px;
  top: 16px;
  bottom: 0;
  width: 1px;
  background: var(--line);
}
.ctx-step-label {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: var(--mono);
}
.ctx-step-mark {
  font-size: 10px;
  flex: none;
}
.ctx-step-mark.done {
  color: var(--success);
}
.ctx-step-mark.fail {
  color: var(--danger);
}
.ctx-empty {
  color: var(--dim);
  font-size: 11px;
}
.ctx-agent-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.ctx-agent-mark {
  width: 24px;
  height: 24px;
  border-radius: 7px;
  background: var(--grad);
  display: grid;
  place-items: center;
  font-size: 10px;
  font-weight: 800;
  color: #04060d;
  flex: none;
}
.ctx-agent-head b {
  font-size: 12.5px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ctx-agent-desc {
  margin: 0;
  font-size: 11px;
  color: var(--muted);
  line-height: 1.6;
}
@keyframes ctxPulse {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.3;
  }
}
@media (max-width: 1100px) {
  .chat-ctx {
    display: none;
  }
}
</style>
