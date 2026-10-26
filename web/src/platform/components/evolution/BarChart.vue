<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

/** 每轮资源消耗柱状图。手写 SVG，零依赖。
 *
 * SVG 宽度跟随容器实际像素，避免非等比缩放导致文字模糊。
 */

const props = withDefaults(
  defineProps<{
    values: number[]
    labels?: string[]
    formatter?: (value: number) => string
    color?: string
  }>(),
  {
    labels: () => [],
    formatter: (value: number) => String(value),
    color: 'var(--p-cyan)',
  },
)

const H = 110
const PAD = { l: 8, r: 8, t: 18, b: 18 }

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

const max = computed(() => Math.max(...props.values, 1))
const bandWidth = computed(
  () => (width.value - PAD.l - PAD.r) / Math.max(props.values.length, 1),
)

const bars = computed(() =>
  props.values.map((value, index) => {
    const height = ((H - PAD.t - PAD.b) * value) / max.value
    const barX = PAD.l + index * bandWidth.value + bandWidth.value * 0.2
    const barWidth = Math.min(bandWidth.value * 0.6, 54)
    return {
      x: barX,
      y: H - PAD.b - height,
      width: barWidth,
      height: Math.max(height, value > 0 ? 2 : 0),
      labelX: barX + barWidth / 2,
      valueText: value > 0 ? props.formatter(value) : '—',
      axisText: props.labels[index] ?? `c${index}`,
    }
  }),
)
</script>

<template>
  <div ref="box" class="evo-bar-box">
    <svg :viewBox="`0 0 ${width} ${H}`" role="img" aria-label="资源消耗柱状图">
      <g v-for="(bar, index) in bars" :key="index">
        <rect
          :x="bar.x"
          :y="bar.y"
          :width="bar.width"
          :height="bar.height"
          rx="3"
          :fill="color"
          opacity=".75"
        />
        <text
          :x="bar.labelX"
          :y="bar.y - 5"
          fill="#a9b6d3"
          font-size="10.5"
          text-anchor="middle"
          font-family="var(--p-mono)"
        >
          {{ bar.valueText }}
        </text>
        <text
          :x="bar.labelX"
          :y="H - 5"
          fill="#8b98b4"
          font-size="10.5"
          text-anchor="middle"
          font-family="var(--p-mono)"
        >
          {{ bar.axisText }}
        </text>
      </g>
    </svg>
  </div>
</template>

<style scoped>
.evo-bar-box {
  height: 110px;
  position: relative;
}
.evo-bar-box svg {
  width: 100%;
  height: 100%;
  display: block;
}
/* styles.css 的全局 svg 规则会继承 1.7px stroke 到文字，必须显式关闭。 */
.evo-bar-box svg text {
  stroke: none;
}
</style>
