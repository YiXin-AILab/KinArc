<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import {
  defaultEvolutionClient,
  type EvolutionTaskCreatePayload,
  type EvolutionTemplate,
} from './evolutionClient'
import { showToast } from './toast'
import { toYaml } from './yaml'

const TASK_ID_PATTERN = /^[a-z][a-z0-9_]{0,63}$/

const client = defaultEvolutionClient()
const router = useRouter()

const templates = ref<EvolutionTemplate[] | null>(null)
const loadError = ref<string | null>(null)

const templateId = ref('')
const taskId = ref('')
const objective = ref('')
const initialTarget = ref('')
const maxIterations = ref('')
const improverTimeoutSeconds = ref('')
const maxCaseConcurrency = ref('')
const maxRetries = ref('')
const rollbackEnabled = ref(true)
const rollbackTouched = ref(false)
const explorationPatience = ref('')
const settingsRows = ref<{ key: string; value: string }[]>([])

const submitting = ref(false)
const submitError = ref<string | null>(null)

/** 回退开关变更；独立方法避免多语句内联 handler。 */
function onRollbackChange(event: Event): void {
  rollbackEnabled.value = (event.target as HTMLInputElement).checked
  rollbackTouched.value = true
}

const selectedTemplate = computed(
  () => (templates.value ?? []).find((tpl) => tpl.task_id === templateId.value) ?? null,
)

const taskIdError = computed(() => {
  if (!taskId.value) return 'task_id 必填'
  if (!TASK_ID_PATTERN.test(taskId.value)) {
    return '格式：^[a-z][a-z0-9_]{0,63}$（小写字母开头，仅小写字母 / 数字 / 下划线）'
  }
  return null
})

const formError = computed(() => {
  if (!templateId.value) return '请选择模板'
  return taskIdError.value
})

const yamlPreview = computed(() => toYaml(buildPayload()))

function onTemplateChange(): void {
  const template = selectedTemplate.value
  if (!template) return
  if (!objective.value) objective.value = template.objective
  if (!initialTarget.value) initialTarget.value = template.initial_target
}

function addSettingsRow(): void {
  settingsRows.value.push({ key: '', value: '' })
}

function removeSettingsRow(index: number): void {
  settingsRows.value.splice(index, 1)
}

function coerceSettingValue(raw: string): unknown {
  const value = raw.trim()
  if (value === 'true') return true
  if (value === 'false') return false
  if (/^-?\d+(\.\d+)?$/.test(value)) return Number(value)
  return raw
}

function parseNumber(raw: string | number): number | undefined {
  if (typeof raw === 'number') return Number.isFinite(raw) ? raw : undefined
  const value = raw.trim()
  if (!value) return undefined
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : undefined
}

function buildPayload(): EvolutionTaskCreatePayload {
  const payload: EvolutionTaskCreatePayload = {
    template_id: templateId.value,
    task_id: taskId.value.trim(),
  }
  const objectiveText = objective.value.trim()
  if (objectiveText) payload.objective = objectiveText
  const targetText = initialTarget.value.trim()
  if (targetText) payload.initial_target = targetText
  const iterations = parseNumber(maxIterations.value)
  if (iterations !== undefined) payload.max_iterations = iterations
  const timeout = parseNumber(improverTimeoutSeconds.value)
  if (timeout !== undefined) payload.improver_timeout_seconds = timeout
  const concurrency = parseNumber(maxCaseConcurrency.value)
  if (concurrency !== undefined) payload.max_case_concurrency = concurrency
  const retries = parseNumber(maxRetries.value)
  if (retries !== undefined) payload.max_retries = retries
  if (rollbackTouched.value) payload.rollback_enabled = rollbackEnabled.value
  const patience = parseNumber(explorationPatience.value)
  if (patience !== undefined) payload.exploration_patience = patience
  const settings: Record<string, unknown> = {}
  for (const row of settingsRows.value) {
    const key = row.key.trim()
    if (key) settings[key] = coerceSettingValue(row.value)
  }
  if (Object.keys(settings).length > 0) payload.settings = settings
  return payload
}

async function submit(startAfterCreate: boolean): Promise<void> {
  submitError.value = null
  if (formError.value) {
    submitError.value = formError.value
    return
  }
  submitting.value = true
  try {
    const task = await client.createTask(buildPayload())
    if (startAfterCreate) {
      await client.startRun(task.task_id)
      showToast(`✓ 已创建并启动 ${task.task_id}`)
    } else {
      showToast(`✓ 已创建 ${task.task_id}`)
    }
    await router.push(`/platform/evolution/${task.task_id}`)
  } catch (caught) {
    const message = caught instanceof Error ? caught.message : '创建失败'
    submitError.value = message
    showToast(message, 'error')
  } finally {
    submitting.value = false
  }
}

async function load(): Promise<void> {
  loadError.value = null
  try {
    templates.value = await client.listTemplates()
    const first = templates.value[0]
    if (first && !templateId.value) {
      templateId.value = first.task_id
      onTemplateChange()
    }
  } catch (caught) {
    loadError.value = caught instanceof Error ? caught.message : '模板加载失败'
  }
}

onMounted(() => void load())
</script>

<template>
  <div class="platform-root">
    <div class="platform-page">
      <div class="platform-page-inner" style="max-width: 1060px">
        <div class="platform-page-head">
          <div>
            <h1>新建自进化优化任务</h1>
            <p
              >从模板克隆一次 Evolution Run：配置迭代策略后，由 evolution-agent 自动改写
              → 评估 → 对比 → 择优。</p
            >
          </div>
          <button class="platform-btn" @click="router.push('/platform/evolution')"
            >← 任务列表</button
          >
        </div>

        <div v-if="loadError" class="platform-state">
          <p role="alert">{{ loadError }}</p>
          <button class="platform-btn" @click="load">重试</button>
        </div>
        <div v-else-if="!templates" class="platform-state">正在加载模板…</div>
        <template v-else>
          <div v-if="submitError" class="platform-field-error" role="alert"
            >✕ {{ submitError }}</div
          >

          <div class="evo-cfg-grid">
            <div class="platform-card evo-cfg-sec">
              <h3>任务与目标</h3>
              <div class="platform-field">
                <label>模板（template_id）</label>
                <select v-model="templateId" @change="onTemplateChange">
                  <option
                    v-for="tpl in templates"
                    :key="tpl.task_id"
                    :value="tpl.task_id"
                  >
                    {{ tpl.task_id }}（{{
                      Object.entries(tpl.dataset_case_counts)
                        .map(([name, count]) => `${name}: ${count}`)
                        .join(' · ')
                    }}）
                  </option>
                </select>
                <div v-if="selectedTemplate" class="platform-hint platform-hint-ok">
                  数据集 case 数：
                  <span
                    v-for="(count, name) in selectedTemplate.dataset_case_counts"
                    :key="name"
                    class="platform-mono"
                    style="margin-right: 10px"
                    >{{ name }}={{ count }}</span
                  >
                </div>
              </div>
              <div class="platform-field">
                <label>任务 ID（task_id）</label>
                <input
                  v-model="taskId"
                  type="text"
                  :class="{ 'platform-input-invalid': taskId && taskIdError }"
                  placeholder="例如 tau3_banking_t35v24"
                />
                <div v-if="taskId && taskIdError" class="platform-hint">{{
                  taskIdError
                }}</div>
              </div>
              <div class="platform-field">
                <label>优化目标（objective）</label>
                <textarea
                  v-model="objective"
                  rows="3"
                  :placeholder="selectedTemplate?.objective ?? '留空则沿用模板'"
                />
              </div>
              <div class="platform-field">
                <label>被优化 Agent（initial_target）</label>
                <input
                  v-model="initialTarget"
                  type="text"
                  :placeholder="
                    selectedTemplate?.initial_target ??
                    'application/xxx，留空则沿用模板'
                  "
                />
                <div class="platform-hint platform-hint-ok"
                  >格式：application/&lt;agent&gt;，留空则沿用模板默认值</div
                >
              </div>
            </div>

            <div class="platform-card evo-cfg-sec">
              <h3>迭代策略</h3>
              <div class="platform-frow2">
                <div class="platform-field">
                  <label>最大迭代轮数（max_iterations）</label>
                  <input
                    v-model="maxIterations"
                    type="number"
                    min="0"
                    placeholder="沿用模板"
                  />
                </div>
                <div class="platform-field">
                  <label>单轮优化超时（improver_timeout_seconds）</label>
                  <input
                    v-model="improverTimeoutSeconds"
                    type="number"
                    min="1"
                    placeholder="沿用模板"
                  />
                </div>
                <div class="platform-field">
                  <label>Case 并发（max_case_concurrency）</label>
                  <input
                    v-model="maxCaseConcurrency"
                    type="number"
                    min="1"
                    placeholder="沿用模板"
                  />
                </div>
                <div class="platform-field">
                  <label>失败重试（max_retries）</label>
                  <input
                    v-model="maxRetries"
                    type="number"
                    min="0"
                    placeholder="沿用模板"
                  />
                </div>
                <div class="platform-field">
                  <label>探索耐心（exploration_patience）</label>
                  <input
                    v-model="explorationPatience"
                    type="number"
                    min="1"
                    placeholder="沿用模板"
                  />
                </div>
                <div class="platform-field">
                  <label>回退策略（rollback_enabled）</label>
                  <div
                    style="
                      display: flex;
                      align-items: center;
                      gap: 10px;
                      padding-top: 6px;
                    "
                  >
                    <label class="platform-switch">
                      <input
                        type="checkbox"
                        :checked="rollbackEnabled"
                        @change="onRollbackChange"
                      />
                      <i />
                    </label>
                    <span class="platform-dim" style="font-size: 12px">
                      {{
                        rollbackTouched
                          ? rollbackEnabled
                            ? '开启：连续劣化时回退到历史最佳'
                            : '关闭'
                          : '未修改（沿用模板）'
                      }}
                    </span>
                  </div>
                </div>
              </div>
            </div>

            <div class="platform-card evo-cfg-sec">
              <h3>settings 覆盖</h3>
              <div
                v-for="(row, index) in settingsRows"
                :key="index"
                class="platform-kvrow"
              >
                <input v-model="row.key" type="text" placeholder="key" />
                <input v-model="row.value" type="text" placeholder="value" />
                <button
                  class="platform-btn platform-btn-sm platform-btn-ghost platform-btn-dngr"
                  @click="removeSettingsRow(index)"
                  >删除</button
                >
              </div>
              <button class="platform-btn platform-btn-sm" @click="addSettingsRow"
                >＋ 添加键值对</button
              >
              <div class="platform-hint platform-hint-ok" style="margin-top: 8px"
                >value 为 true / false / 数字时自动转换类型，其余按字符串提交</div
              >
            </div>

            <div class="platform-card evo-cfg-sec">
              <h3
                >config.yaml 预览
                <span
                  class="platform-dim platform-mono"
                  style="margin-left: auto; font-size: 11px; font-weight: 400"
                  >只读 · 随表单实时更新</span
                ></h3
              >
              <pre class="platform-yaml evo-yaml" data-testid="yaml-preview">{{
                yamlPreview
              }}</pre>
            </div>
          </div>

          <div class="evo-cfg-foot">
            <button class="platform-btn" :disabled="submitting" @click="submit(false)">
              {{ submitting ? '提交中…' : '创建' }}
            </button>
            <button
              class="platform-btn platform-btn-pri"
              :disabled="submitting"
              @click="submit(true)"
            >
              {{ submitting ? '提交中…' : '▶ 创建并启动' }}
            </button>
          </div>
        </template>
      </div>
    </div>
  </div>
</template>

<style scoped>
.evo-cfg-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}
.evo-cfg-sec {
  padding: 18px 20px;
}
.evo-cfg-sec h3 {
  font-size: 13.5px;
  font-weight: 600;
  margin: 0 0 14px;
  display: flex;
  align-items: center;
  gap: 8px;
}
.evo-yaml {
  background: rgba(4, 6, 13, 0.55);
  border: 1px solid var(--p-border);
  border-radius: 10px;
  max-height: 320px;
  overflow: auto;
}
.evo-cfg-foot {
  display: flex;
  justify-content: flex-end;
  gap: 12px;
  margin-top: 20px;
}
@media (max-width: 1000px) {
  .evo-cfg-grid {
    grid-template-columns: 1fr;
  }
}
</style>
