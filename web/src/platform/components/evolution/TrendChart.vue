<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

/** 评分趋势折线图：train 绿 / validation 琥珀 / best 基线紫虚线。手写 SVG，零依赖。
 *
 * SVG 尺寸跟随容器实际像素（1 viewBox 单位 = 1 CSS 像素），
 * 避免 viewBox + preserveAspectRatio="none" 拉伸导致的文字模糊。
 */

export interface TrendPoint {
  train: number | null
  validation: number | null
}

const props = defineProps<{
  points: TrendPoint[]
  best?: number | null
  labels?: string[]
}>()

const H = 200
const PAD = { l: 38, r: 14, t: 12, b: 24 }

const box = ref<HTMLElement | null>(null)
const width = ref(640)
let observer: ResizeObserver | null = null

onMounted(() => {
  if (!box.value) return
  width.value = box.value.clientWidth || width.value
  // jsdom 等环境没有 ResizeObserver，退化为挂载时的静态宽度。
  if (typeof ResizeObserver === 'undefined') return
  observer = new ResizeObserver((entries) => {
    const entry = entries[0]
    if (entry && entry.contentRect.width > 0) {
      width.value = entry.contentRect.width
    }
  })
  observer.observe(box.value)
})

onBeforeUnmount(() => {
  observer?.disconnect()
})

const innerWidth = computed(() => width.value - PAD.l - PAD.r)
const innerHeight = H - PAD.t - PAD.b

const count = computed(() => Math.max(props.points.length, 1))

function x(index: number): number {
  if (count.value <= 1) return PAD.l + innerWidth.value / 2
  return PAD.l + (index / (count.value - 1)) * innerWidth.value
}

function y(value: number): number {
  return PAD.t + (1 - clamp(value)) * innerHeight
}

function clamp(value: number): number {
  return Math.min(Math.max(value, 0), 1)
}

const gridValues = [0, 0.25, 0.5, 0.75, 1]

function linePoints(pick: (point: TrendPoint) => number | null): string {
  const parts: string[] = []
  props.points.forEach((point, index) => {
    const value = pick(point)
    if (value !== null) parts.push(`${x(index)},${y(value)}`)
  })
  return parts.length > 1 ? parts.join(' ') : ''
}

const trainLine = computed(() => linePoints((point) => point.train))
const validationLine = computed(() => linePoints((point) => point.validation))

const trainDots = computed(() =>
  props.points.flatMap((point, index) =>
    point.train === null ? [] : [{ cx: x(index), cy: y(point.train) }],
  ),
)
const validationDots = computed(() =>
  props.points.flatMap((point, index) =>
    point.validation === null ? [] : [{ cx: x(index), cy: y(point.validation) }],
  ),
)

const bestY = computed(() =>
  props.best !== null && props.best !== undefined && props.best > 0
    ? y(props.best)
    : null,
)

const xLabels = computed(() =>
  props.points.map((_, index) => ({
    x: x(index),
    text: props.labels?.[index] ?? `c${index}`,
  })),
)
</script>

<template>
  <div>
    <div ref="box" class="evo-chart-box">
      <svg :viewBox="`0 0 ${width} ${H}`" role="img" aria-label="评分趋势">
        <g v-for="value in gridValues" :key="value">
          <line
            :x1="PAD.l"
            :y1="y(value)"
            :x2="width - PAD.r"
            :y2="y(value)"
            stroke="rgba(148,163,255,.08)"
          />
          <text
            :x="PAD.l - 6"
            :y="y(value) + 3.5"
            fill="#8b98b4"
            font-size="10.5"
            text-anchor="end"
            font-family="var(--p-mono)"
          >
            {{ Math.round(value * 100) }}%
          </text>
        </g>
        <text
          v-for="label in xLabels"
          :key="label.x"
          :x="label.x"
          :y="H - 7"
          fill="#8b98b4"
          font-size="10.5"
          text-anchor="middle"
          font-family="var(--p-mono)"
        >
          {{ label.text }}
        </text>
        <line
          v-if="bestY !== null"
          :x1="PAD.l"
          :y1="bestY"
          :x2="width - PAD.r"
          :y2="bestY"
          stroke="var(--p-violet)"
          stroke-width="1.2"
          stroke-dasharray="5 4"
          opacity=".8"
        />
        <polyline
          v-if="trainLine"
          :points="trainLine"
          fill="none"
          stroke="var(--p-green)"
          stroke-width="2"
          stroke-linejoin="round"
        />
        <polyline
          v-if="validationLine"
          :points="validationLine"
          fill="none"
          stroke="var(--p-amber)"
          stroke-width="2"
          stroke-linejoin="round"
        />
        <circle
          v-for="(dot, index) in trainDots"
          :key="`t-${index}`"
          :cx="dot.cx"
          :cy="dot.cy"
          r="3.2"
          fill="var(--p-green)"
        />
        <circle
          v-for="(dot, index) in validationDots"
          :key="`v-${index}`"
          :cx="dot.cx"
          :cy="dot.cy"
          r="3.2"
          fill="var(--p-amber)"
        />
      </svg>
    </div>
    <div class="evo-legend">
      <span><i style="background: var(--p-green)" />train</span>
      <span><i style="background: var(--p-amber)" />validation</span>
      <span><i style="background: var(--p-violet)" />best 基线</span>
    </div>
  </div>
</template>

<style scoped>
.evo-chart-box {
  height: 200px;
  position: relative;
}
.evo-chart-box svg {
  width: 100%;
  height: 100%;
  display: block;
}
/* styles.css 的全局 svg 规则会继承 1.7px stroke 到文字，必须显式关闭。 */
.evo-chart-box svg text {
  stroke: none;
}
.evo-legend {
  display: flex;
  gap: 16px;
  font-size: 11.5px;
  color: var(--p-mut);
  font-family: var(--p-mono);
  margin-top: 6px;
}
.evo-legend i {
  display: inline-block;
  width: 14px;
  height: 3px;
  border-radius: 2px;
  margin-right: 5px;
  vertical-align: middle;
}
</style>
