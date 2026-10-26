<script setup lang="ts">
/** 候选版本流水线卡片：五段阶段条（改进 / 发布 / 训练执行 / 训练评分 / 验证评分）。 */

export type PipelineStageState = 'pending' | 'run' | 'ok' | 'skip' | 'fail'

export interface PipelineBadge {
  label: string
  cls: string
}

export interface PipelineCandidate {
  id: string
  stages: PipelineStageState[]
  badges: PipelineBadge[]
  scoreLine?: string
  note?: string
  active?: boolean
  best?: boolean
}

defineProps<{
  candidates: PipelineCandidate[]
}>()

const STAGE_NAMES = ['改进', '发布', '训练执行', '训练评分', '验证评分']
</script>

<template>
  <div>
    <div
      v-for="candidate in candidates"
      :key="candidate.id"
      class="evo-cand"
      :class="{ active: candidate.active, best: candidate.best }"
      :data-candidate="candidate.id"
    >
      <div class="evo-cand-top">
        <span class="cid">{{ candidate.id }}</span>
        <span
          v-for="badge in candidate.badges"
          :key="badge.label"
          class="platform-badge"
          :class="badge.cls"
        >
          {{ badge.label }}
        </span>
        <span class="cscore">{{ candidate.scoreLine ?? '' }}</span>
      </div>
      <div class="evo-stages">
        <div
          v-for="(name, index) in STAGE_NAMES"
          :key="name"
          class="evo-stage"
          :class="candidate.stages[index] ?? 'pending'"
        >
          <div class="st-bar" />
          <div class="st-name">{{ name }}</div>
        </div>
      </div>
      <div v-if="candidate.note" class="evo-cand-note">{{ candidate.note }}</div>
    </div>
    <div
      v-if="candidates.length === 0"
      class="platform-mut"
      style="font-size: 12.5px; padding: 8px 0"
    >
      等待第一个候选版本…
    </div>
  </div>
</template>

<style scoped>
.evo-cand {
  border: 1px solid var(--p-border);
  border-radius: 12px;
  padding: 12px 14px;
  margin-bottom: 12px;
  background: rgba(4, 6, 13, 0.35);
  transition: 0.2s;
  position: relative;
}
.evo-cand.active {
  border-color: rgba(34, 211, 238, 0.45);
  box-shadow: 0 0 0 3px rgba(34, 211, 238, 0.07);
}
.evo-cand.best {
  border-color: rgba(251, 191, 36, 0.4);
}
.evo-cand-top {
  display: flex;
  align-items: center;
  gap: 9px;
  margin-bottom: 9px;
}
.evo-cand-top .cid {
  font-family: var(--p-mono);
  font-size: 12.5px;
  font-weight: 600;
}
.evo-cand-top .cscore {
  margin-left: auto;
  font-family: var(--p-mono);
  font-size: 12px;
  color: var(--p-mut);
}
.evo-stages {
  display: flex;
  gap: 5px;
}
.evo-stage {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
  position: relative;
}
.evo-stage .st-bar {
  height: 4px;
  width: 100%;
  border-radius: 99px;
  background: rgba(148, 163, 255, 0.12);
  transition: 0.3s;
}
.evo-stage .st-name {
  font-size: 10px;
  color: var(--p-dim);
  font-family: var(--p-mono);
  transform: scale(0.92);
}
.evo-stage.run .st-bar {
  background: var(--p-cyan);
  box-shadow: 0 0 8px rgba(34, 211, 238, 0.7);
  animation: evo-pulse 1.1s ease-in-out infinite;
}
.evo-stage.ok .st-bar {
  background: var(--p-green);
}
.evo-stage.skip .st-bar {
  background: var(--p-dim);
}
.evo-stage.fail .st-bar {
  background: var(--p-red);
}
.evo-stage.run .st-name {
  color: var(--p-cyan);
}
.evo-stage.ok .st-name {
  color: var(--p-mut);
}
@keyframes evo-pulse {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.35;
  }
}
.evo-cand-note {
  font-size: 11px;
  color: var(--p-dim);
  margin-top: 8px;
  font-family: var(--p-mono);
}
</style>
