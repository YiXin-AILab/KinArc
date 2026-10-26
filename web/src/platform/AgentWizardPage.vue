<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import type { AgentSummary, Catalog, Issue } from '../api/platformClient'
import { defaultPlatformClient } from './client'
import AgentCapabilitiesSection from './components/AgentCapabilitiesSection.vue'
import AgentCompactionSection from './components/AgentCompactionSection.vue'
import AgentKnowledgeSection from './components/AgentKnowledgeSection.vue'
import AgentModelSection from './components/AgentModelSection.vue'
import AgentOutputSection from './components/AgentOutputSection.vue'
import AgentPromptSection from './components/AgentPromptSection.vue'
import YamlPreview from './components/YamlPreview.vue'
import { showToast } from './toast'
import { toYaml } from './yaml'
import { useAgentForm, validateFormLocal } from './useAgentForm'

const route = useRoute()
const router = useRouter()
const client = defaultPlatformClient()
const { form, reset, toConfig } = useAgentForm()

const STEP_TITLES = ['起始方式', 'Prompt', '模型', '能力', '输出与压缩', '确认创建']

const step = ref(1)
const maxReached = ref(1)
const startMode = ref<'template' | 'scratch'>('template')
const templateId = ref('')
const agents = ref<AgentSummary[]>([])
const catalog = ref<Catalog | null>(null)
const stepIssues = ref<Issue[]>([])
const creating = ref(false)
const sourceLabel = ref('从零创建')

async function loadTemplates(): Promise<void> {
  try {
    agents.value = await client.listAgents()
    const from = typeof route.query.from === 'string' ? route.query.from : ''
    if (from && agents.value.some((agent) => agent.agent_id === from)) {
      templateId.value = from
    } else {
      templateId.value = agents.value[0]?.agent_id ?? ''
    }
    if (templateId.value) await applyTemplate()
  } catch (caught) {
    showToast(caught instanceof Error ? caught.message : 'Agent 列表加载失败', 'error')
  }
  catalog.value = await client.catalog().catch(() => null)
}

/** 切到“从零创建”模式并清空表单；独立方法避免多语句内联 handler。 */
function chooseScratch(): void {
  startMode.value = 'scratch'
  reset()
}

async function applyTemplate(): Promise<void> {
  if (!templateId.value) return
  try {
    const detail = await client.getAgent(templateId.value)
    const keepId = form.agentId
    const keepName = form.agentName
    reset(detail.config, detail.prompt_text)
    form.agentId = keepId
    form.agentName = keepName
    const source = agents.value.find((agent) => agent.agent_id === templateId.value)
    sourceLabel.value = `复制自 ${source?.agent_name ?? templateId.value}`
    showToast(`✓ 已载入模板 ${templateId.value}`)
  } catch (caught) {
    showToast(caught instanceof Error ? caught.message : '模板加载失败', 'error')
  }
}

function validateStep(target: number): boolean {
  let issues: Issue[] = []
  if (target === 1) {
    issues = validateFormLocal(form, {
      existingAgentIds: agents.value.map((agent) => agent.agent_id),
    }).filter((issue) => issue.field === 'agent_id' || issue.field === 'agent_name')
    if (startMode.value === 'template' && !templateId.value) {
      issues.push({ field: 'template', message: '请选择要复制的模板 Agent' })
    }
  } else if (target === 2) {
    if (!form.promptText.trim()) {
      issues = [{ field: 'prompt', message: '系统 Prompt 不能为空' }]
    }
  } else if (target === 3) {
    issues = validateFormLocal(form).filter((issue) =>
      issue.field.startsWith('llm_model'),
    )
  } else if (target === 5) {
    issues = validateFormLocal(form).filter(
      (issue) =>
        issue.field.startsWith('compaction') || issue.field.startsWith('outbound'),
    )
  }
  stepIssues.value = issues
  return issues.length === 0
}

function goStep(target: number): void {
  const clamped = Math.max(1, Math.min(6, target))
  if (clamped > step.value && !validateStep(step.value)) return
  if (clamped > maxReached.value + 1) return
  step.value = clamped
  maxReached.value = Math.max(maxReached.value, clamped)
  stepIssues.value = []
}

function next(): void {
  if (step.value === 6) {
    void create()
    return
  }
  goStep(step.value + 1)
}

const summary = computed(() => {
  const globalCount = form.enabledSkills.filter((name) =>
    catalog.value?.global_skills.some((skill) => skill.name === name),
  ).length
  return {
    model: form.model || '未设置',
    tools: form.enabledTools.length,
    globalSkills: globalCount,
    privateSkills: form.enabledSkills.length - globalCount,
    output: `${form.outputMode} / ${form.outboundType}`,
  }
})

const yamlText = computed(
  () => `# 将由平台生成 · 重启后生效\n${toYaml(toConfig().config)}`,
)

async function create(): Promise<void> {
  const local = validateFormLocal(form, {
    requirePrompt: true,
    existingAgentIds: agents.value.map((agent) => agent.agent_id),
  })
  if (local.length > 0) {
    stepIssues.value = local
    showToast('存在校验错误，请返回对应步骤修正', 'error')
    return
  }
  creating.value = true
  try {
    const { config } = toConfig()
    const result = await client.validateAgent({ config, prompt_text: form.promptText })
    if (result.errors.length > 0) {
      stepIssues.value = result.errors
      showToast('服务端校验未通过，已阻断创建', 'error')
      return
    }
    const agent = await client.createAgent({ config, prompt_text: form.promptText })
    showToast(`✓ 已创建 ${agent.agent_id} · 重启后生效`)
    router.push(`/platform/agents/${agent.agent_id}/edit`)
  } catch (caught) {
    showToast(caught instanceof Error ? caught.message : '创建失败', 'error')
  } finally {
    creating.value = false
  }
}

onMounted(() => void loadTemplates())
</script>

<template>
  <div class="platform-root">
    <div class="platform-editor">
      <div class="platform-editor-topbar">
        <button
          class="platform-btn platform-btn-ghost platform-btn-sm"
          @click="router.push('/platform/agents')"
          >← 取消</button
        >
        <b>新建 Agent</b>
        <span class="platform-badge platform-b-violet">向导式创建</span>
        <span class="platform-mut" style="font-size: 12px"
          >每步即时校验，最后一步统一生成 agent.yaml</span
        >
        <span
          class="platform-mono platform-dim"
          style="margin-left: auto; font-size: 12px"
          >第 {{ step }} / 6 步</span
        >
      </div>

      <div class="platform-wsteps">
        <template v-for="(title, index) in STEP_TITLES" :key="title">
          <div
            class="platform-ws"
            :class="{ on: step === index + 1, done: index + 1 < step }"
            @click="goStep(index + 1)"
          >
            <i>{{ index + 1 < step ? '✓' : index + 1 }}</i
            ><span>{{ title }}</span>
          </div>
          <div
            v-if="index < 5"
            class="platform-wline"
            :class="{ done: index + 1 < step }"
          />
        </template>
      </div>

      <div style="flex: 1; overflow-y: auto; min-height: 0">
        <div
          v-if="stepIssues.length"
          style="max-width: 820px; margin: 0 auto; padding: 14px 24px 0"
        >
          <div
            v-for="(issue, index) in stepIssues"
            :key="index"
            class="platform-field-error"
            style="margin-bottom: 8px"
          >
            ✕ {{ issue.field }}：{{ issue.message }}
          </div>
        </div>

        <div v-if="step === 1" class="platform-wpane">
          <h2>从哪开始？</h2>
          <p class="platform-wsub"
            >新增业务场景最常见的路径是复制一个相近的现有 Agent，再改 Prompt 和工具</p
          >
          <div
            style="
              display: grid;
              grid-template-columns: 1fr 1fr;
              gap: 14px;
              margin-bottom: 22px;
            "
          >
            <div
              class="platform-bigopt"
              :class="{ on: startMode === 'template' }"
              @click="startMode = 'template'"
            >
              <div style="font-size: 22px; margin-bottom: 8px">⧉</div>
              <b>从模板复制</b>
              <p class="platform-mut" style="font-size: 12.5px; margin: 4px 0 12px"
                >复制现有 Agent
                的全部配置（Prompt、工具、压缩参数），改个名字即可起步</p
              >
              <select
                v-model="templateId"
                aria-label="选择模板"
                @click.stop
                @change="applyTemplate"
              >
                <option
                  v-for="agent in agents"
                  :key="agent.agent_id"
                  :value="agent.agent_id"
                >
                  {{ agent.agent_id }}（{{ agent.agent_name }} ·
                  {{ agent.tool_count }} 工具）
                </option>
              </select>
            </div>
            <div
              class="platform-bigopt"
              :class="{ on: startMode === 'scratch' }"
              @click="chooseScratch"
            >
              <div style="font-size: 22px; margin-bottom: 8px">✦</div>
              <b>从零创建</b>
              <p class="platform-mut" style="font-size: 12.5px; margin: 4px 0 0"
                >空白配置，逐项填写。适合与现有场景差异较大的新 Agent</p
              >
            </div>
          </div>
          <div class="platform-frow2">
            <div class="platform-field">
              <label
                >agent_id
                <span class="platform-mut" style="font-weight: 400"
                  >· 即目录名与路由 key，创建后不可改</span
                ></label
              >
              <input
                v-model="form.agentId"
                type="text"
                class="platform-mono"
                placeholder="loan-advisor"
                aria-label="agent_id"
              />
            </div>
            <div class="platform-field">
              <label
                >agent_name
                <span class="platform-mut" style="font-weight: 400"
                  >· 展示名称</span
                ></label
              >
              <input
                v-model="form.agentName"
                type="text"
                placeholder="loan_advisor"
                aria-label="agent_name"
              />
            </div>
          </div>
        </div>

        <div v-else-if="step === 2" class="platform-wpane">
          <h2>写系统 Prompt</h2>
          <p class="platform-wsub"
            >保存为 prompt/system.md，支持 ${ENV_VAR}
            插值；模板复制时已预填，可在此基础上修改</p
          >
          <AgentPromptSection :form="form" />
        </div>

        <div v-else-if="step === 3" class="platform-wpane">
          <h2>选择模型与生成参数</h2>
          <p class="platform-wsub"
            >LiteLLM Provider · 密钥只引用环境变量，平台不存明文</p
          >
          <AgentModelSection :form="form" />
        </div>

        <div v-else-if="step === 4" class="platform-wpane">
          <h2>勾选能力</h2>
          <p class="platform-wsub"
            >Skill 可选通用库；工具来自已注册 Plugin（新建私有 Skill 可稍后在 Skill
            仓库补）</p
          >
          <AgentCapabilitiesSection
            :form="form"
            :catalog="catalog"
            :private-skill-names="[]"
          />
          <AgentKnowledgeSection :form="form" />
        </div>

        <div v-else-if="step === 5" class="platform-wpane">
          <h2>输出方式与上下文压缩</h2>
          <p class="platform-wsub"
            >不确定就保持默认：stream + 内置 SSE 投影器 + 启用压缩</p
          >
          <AgentOutputSection :form="form" :catalog="catalog" />
          <AgentCompactionSection :form="form" />
        </div>

        <div v-else class="platform-wpane" style="max-width: 1000px">
          <h2>确认并创建</h2>
          <p class="platform-wsub"
            >创建即写入 application/{{
              form.agentId || '&lt;agent_id&gt;'
            }}/config/agent.yaml，重启后生效</p
          >
          <div style="display: grid; grid-template-columns: 1fr 1.2fr; gap: 16px">
            <div>
              <div
                class="platform-card"
                style="padding: 16px 18px; margin-bottom: 12px"
              >
                <div
                  class="platform-dim"
                  style="font-size: 11px; letter-spacing: 1px; margin-bottom: 10px"
                  >配置摘要</div
                >
                <div class="platform-sumrow"
                  ><span>Agent</span
                  ><b class="platform-mono">{{ form.agentId || '—' }}</b></div
                >
                <div class="platform-sumrow"
                  ><span>来源</span><b>{{ sourceLabel }}</b></div
                >
                <div class="platform-sumrow"
                  ><span>模型</span
                  ><b class="platform-mono">{{ summary.model }}</b></div
                >
                <div class="platform-sumrow"
                  ><span>工具</span><b>{{ summary.tools }} 项</b></div
                >
                <div class="platform-sumrow"
                  ><span>Skill</span
                  ><b
                    >{{ summary.globalSkills }} 通用 ·
                    {{ summary.privateSkills }} 私有</b
                  ></div
                >
                <div class="platform-sumrow"
                  ><span>输出</span
                  ><b class="platform-mono">{{ summary.output }}</b></div
                >
              </div>
            </div>
            <YamlPreview
              :yaml="yamlText"
              class="platform-card"
              style="max-height: 340px; overflow-y: auto"
            />
          </div>
        </div>
      </div>

      <div class="platform-wfoot">
        <button
          class="platform-btn"
          :style="{ visibility: step === 1 ? 'hidden' : 'visible' }"
          @click="goStep(step - 1)"
          >← 上一步</button
        >
        <span class="platform-dim" style="font-size: 12px"
          >校验在每一步即时进行 · 未完成的步骤不会写入磁盘</span
        >
        <button
          class="platform-btn platform-btn-pri"
          :disabled="creating"
          @click="next"
        >
          {{
            step === 6
              ? creating
                ? '创建中…'
                : '✓ 创建 Agent（重启后生效）'
              : '下一步 →'
          }}
        </button>
      </div>
    </div>
  </div>
</template>
