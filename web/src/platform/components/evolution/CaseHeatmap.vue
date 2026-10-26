<script setup lang="ts">
import { computed } from 'vue'

/** Train case 通过矩阵：行 = case_id，列 = 候选。绿 ✓ / 红 ✗ / 灰 = 无数据。 */

export interface HeatmapColumn {
  label: string
  /** case_id → score；缺失表示该轮未执行。 */
  results: Record<string, number>
}

const props = defineProps<{
  caseIds: string[]
  columns: HeatmapColumn[]
}>()

type CellKind = 'pass' | 'fail' | 'none'

function cellKind(column: HeatmapColumn, caseId: string): CellKind {
  const score = column.results[caseId]
  if (score === undefined) return 'none'
  return score >= 1 ? 'pass' : 'fail'
}

const rows = computed(() =>
  props.caseIds.map((caseId) => ({
    caseId,
    cells: props.columns.map((column) => cellKind(column, caseId)),
  })),
)
</script>

<template>
  <div style="overflow-x: auto">
    <table class="evo-heat">
      <thead>
        <tr>
          <th />
          <th v-for="column in columns" :key="column.label">{{ column.label }}</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="row.caseId">
          <td class="cn">{{ row.caseId }}</td>
          <td v-for="(cell, index) in row.cells" :key="index">
            <div class="hc" :class="`p-${cell}`">
              {{ cell === 'pass' ? '✓' : cell === 'fail' ? '✗' : '·' }}
            </div>
          </td>
        </tr>
      </tbody>
    </table>
    <div class="platform-dim platform-mono" style="font-size: 10.5px; margin-top: 8px">
      ✓ 通过 · ✗ 失败 · 灰 = 该轮未执行（skipped / failed）
    </div>
  </div>
</template>

<style scoped>
.evo-heat {
  font-family: var(--p-mono);
  font-size: 10.5px;
  border-collapse: collapse;
}
.evo-heat th {
  color: var(--p-dim);
  font-weight: 400;
  padding: 4px 3px;
}
.evo-heat td {
  padding: 2px;
}
.evo-heat .hc {
  width: 30px;
  height: 20px;
  border-radius: 5px;
  display: grid;
  place-items: center;
  font-size: 10px;
}
.evo-heat .p-pass {
  background: rgba(52, 211, 153, 0.18);
  color: var(--p-green);
}
.evo-heat .p-fail {
  background: rgba(248, 113, 113, 0.15);
  color: var(--p-red);
}
.evo-heat .p-none {
  background: rgba(148, 163, 255, 0.06);
  color: var(--p-dim);
}
.evo-heat .cn {
  color: var(--p-mut);
  padding-right: 12px;
  white-space: nowrap;
}
</style>
