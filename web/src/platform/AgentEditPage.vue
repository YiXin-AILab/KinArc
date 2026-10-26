<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import type { Catalog, Issue } from '../api/platformClient'
import { defaultPlatformClient } from './client'
import AgentAdvancedSection from './components/AgentAdvancedSection.vue'
import AgentBasicSection from './components/AgentBasicSection.vue'
import AgentCapabilitiesSection from './components/AgentCapabilitiesSection.vue'
import AgentCompactionSection from './components/AgentCompactionSection.vue'
import AgentKnowledgeSection from './components/AgentKnowledgeSection.vue'
import AgentModelSection from './components/AgentModelSection.vue'
import AgentOutputSection from './components/AgentOutputSection.vue'
import AgentPromptSection from './components/AgentPromptSection.vue'
import ValidationList from './components/ValidationList.vue'
import YamlPreview from './components/YamlPreview.vue'
import { showToast } from './toast'
import { sectionForField, useAgentForm, validateFormLocal } from './useAgentForm'
import { toYaml } from './yaml'

const route = useRoute()
const router = useRouter()
const client = defaultPlatformClient()
const { form, reset, toConfig } = useAgentForm()

const agentId = String(route.params.id)
const agentName = ref('')
const status = ref<string>('')
const loadError = ref<string | null>(null)
const loaded = ref(false)
const dirty = ref(false)
const saving = ref(false)
const validating = ref(false)
const catalog = ref<Catalog | null>(null)
const privateSkillNames = ref<string[]>([])
const serverErrors = ref<Issue[]>([])
const serverWarnings = ref<Issue[]>([])
const checked = ref(false)
const rightTab = ref<'yaml' | 'check' | 'chat'>('yaml')
const formColumn = ref<HTMLElement | null>(null)
const activeSection = ref('sec-basic')

const sections = [
  { id: 'sec-basic', label: '基本信息' },
  { id: 'sec-prompt', label: 'Prompt' },
  { id: 'sec-model', label: '模型' },
  { id: 'sec-cap', label: '能力' },
  { id: 'sec-kb', label: '知识库' },
  { id: 'sec-out', label: '输出' },
  { id: 'sec-compact', label: '上下文压缩' },
  { id: 'sec-adv', label: '高级' },
]

const allIssues = computed(() => {
  const local = loaded.value ? validateFormLocal(form) : []
  return { errors: [...local, ...serverErrors.value], warnings: serverWarnings.value }
})

const sectionErrorCounts = computed(() => {
  const counts = new Map<string, number>()
  for (const issue of allIssues.value.errors) {
    const section = sectionForField(issue.field)
    counts.set(section, (counts.get(section) ?? 0) + 1)
  }
  return counts
})

const modelError = computed(
  () =>
    allIssues.value.errors.find((issue) => issue.field.startsWith('llm_model'))
      ?.message ?? null,
)

const yamlText = computed(() => {
  if (!loaded.value) return ''
  const { config } = toConfig()
  return `# 生成式写回 · 重启后生效\n${toYaml(config)}`
})

const statusMeta = computed(() => {
  if (status.value === 'active') return { label: '生效中', cls: 'platform-b-green' }
  if (status.value === 'pending')
    return { label: '待重启生效', cls: 'platform-b-amber' }
  if (status.value === 'invalid') return { label: '配置无效', cls: 'platform-b-red' }
  return null
})

async function load(): Promise<void> {
  loadError.value = null
  try {
    const [detail, catalogResult, skills] = await Promise.all([
      client.getAgent(agentId),
      client.catalog().catch(() => null),
      client.listSkills(agentId).catch(() => null),
    ])
    reset(detail.config, detail.prompt_text)
    agentName.value =
      typeof detail.config.agent_name === 'string' && detail.config.agent_name
        ? detail.config.agent_name
        : agentId
    status.value = detail.status
    catalog.value = catalogResult
    privateSkillNames.value = (skills?.skills ?? [])
      .filter((skill) => skill.source === 'private')
      .map((skill) => skill.name)
    loaded.value = true
    dirty.value = false
  } catch (caught) {
    loadError.value = caught instanceof Error ? caught.message : 'Agent 配置加载失败'
  }
}

function markDirty(): void {
  dirty.value = true
}

function scrollToSection(sectionId: string): void {
  activeSection.value = sectionId
  formColumn.value
    ?.querySelector(`#${sectionId}`)
    ?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

function onFormScroll(): void {
  const column = formColumn.value
  if (!column) return
  const nodes = column.querySelectorAll('.platform-fsec')
  let current = 'sec-basic'
  for (const node of nodes) {
    if (node.getBoundingClientRect().top < 200) current = node.id
  }
  activeSection.value = current
}

async function runValidate(): Promise<boolean> {
  const local = validateFormLocal(form, { requirePrompt: true })
  if (local.length > 0) {
    serverErrors.value = []
    serverWarnings.value = []
    checked.value = true
    rightTab.value = 'check'
    return false
  }
  validating.value = true
  try {
    const { config } = toConfig()
    const result = await client.validateAgent({ config, prompt_text: form.promptText })
    serverErrors.value = result.errors
    serverWarnings.value = result.warnings
    checked.value = true
    rightTab.value = 'check'
    return result.errors.length === 0
  } catch (caught) {
    showToast(caught instanceof Error ? caught.message : '校验请求失败', 'error')
    return false
  } finally {
    validating.value = false
  }
}

async function save(): Promise<void> {
  const ok = await runValidate()
  if (!ok) {
    showToast('存在校验错误，已阻断保存', 'error')
    return
  }
  saving.value = true
  try {
    const { config } = toConfig()
    const agent = await client.updateAgent(agentId, {
      config,
      prompt_text: form.promptText,
    })
    status.value = agent.status
    dirty.value = false
    showToast('✓ 已写入 agent.yaml · 重启后生效')
  } catch (caught) {
    showToast(caught instanceof Error ? caught.message : '保存失败', 'error')
  } finally {
    saving.value = false
  }
}

onMounted(() => void load())
</script>

<template>
  <div class="platform-root">
    <div v-if="loadError" class="platform-state">
      <p role="alert">{{ loadError }}</p>
      <button class="platform-btn" @click="load">重试</button>
    </div>
    <div v-else-if="!loaded" class="platform-state">正在加载配置…</div>
    <div v-else class="platform-editor" @input="markDirty" @change="markDirty">
      <div class="platform-editor-topbar">
        <button
          class="platform-btn platform-btn-ghost platform-btn-sm"
          @click="router.push('/platform/agents')"
          >← 返回</button
        >
        <b>{{ agentName }}</b>
        <span class="platform-mono platform-dim" style="font-size: 11px"
          >application/{{ agentId }}/config/agent.yaml</span
        >
        <span v-if="statusMeta" class="platform-badge" :class="statusMeta.cls">{{
          statusMeta.label
        }}</span>
        <span v-if="dirty" class="platform-badge platform-b-amber">● 有未保存修改</span>
        <div class="platform-editor-topbar-actions">
          <button
            class="platform-btn platform-btn-sm"
            :disabled="validating"
            @click="runValidate"
          >
            {{ validating ? '校验中…' : '校验' }}
          </button>
          <button
            class="platform-btn platform-btn-sm platform-btn-pri"
            :disabled="saving"
            @click="save"
          >
            {{ saving ? '保存中…' : '保存（重启后生效）' }}
          </button>
        </div>
      </div>
      <div class="platform-editor-grid">
        <div class="platform-editor-side">
          <div class="platform-side-label">配置区块</div>
          <div
            v-for="section in sections"
            :key="section.id"
            class="platform-secnav"
            :class="{ on: activeSection === section.id }"
            @click="scrollToSection(section.id)"
          >
            {{ section.label }}
            <span
              v-if="sectionErrorCounts.get(section.id)"
              class="platform-badge platform-b-red"
              style="margin-left: auto; padding: 0 6px"
              >{{ sectionErrorCounts.get(section.id) }}</span
            >
          </div>
          <div
            style="
              margin-top: 20px;
              padding: 12px 10px;
              border-top: 1px solid var(--p-border);
            "
          >
            <div class="platform-side-label" style="padding: 0 0 8px">资产</div>
            <div
              class="platform-secnav"
              style="font-size: 12.5px"
              @click="router.push('/platform/skills')"
              >Skill 仓库 ↗</div
            >
            <div
              class="platform-secnav"
              style="font-size: 12.5px"
              @click="router.push('/platform/knowledge')"
              >知识库 ↗</div
            >
          </div>
        </div>

        <div ref="formColumn" class="platform-editor-form" @scroll="onFormScroll">
          <AgentBasicSection :form="form" lock-agent-id />
          <AgentPromptSection :form="form" />
          <AgentModelSection :form="form" :model-error="modelError" />
          <AgentCapabilitiesSection
            :form="form"
            :catalog="catalog"
            :private-skill-names="privateSkillNames"
            :current-agent-id="agentId"
          />
          <AgentKnowledgeSection :form="form" :current-agent-id="agentId" />
          <AgentOutputSection :form="form" :catalog="catalog" />
          <AgentCompactionSection :form="form" />
          <AgentAdvancedSection :form="form" />
        </div>

        <div class="platform-editor-right">
          <div class="platform-rtabs">
            <div
              class="platform-rtab"
              :class="{ on: rightTab === 'yaml' }"
              @click="rightTab = 'yaml'"
              >YAML 预览</div
            >
            <div
              class="platform-rtab"
              :class="{ on: rightTab === 'check' }"
              @click="rightTab = 'check'"
            >
              校验
              <span
                v-if="allIssues.errors.length"
                class="platform-badge platform-b-red"
                style="padding: 0 6px"
                >{{ allIssues.errors.length }}</span
              >
              <span
                v-else-if="allIssues.warnings.length"
                class="platform-badge platform-b-amber"
                style="padding: 0 6px"
                >{{ allIssues.warnings.length }}</span
              >
            </div>
            <div
              class="platform-rtab"
              :class="{ on: rightTab === 'chat' }"
              @click="rightTab = 'chat'"
              >试聊</div
            >
          </div>
          <div v-show="rightTab === 'yaml'" class="platform-rpane">
            <YamlPreview :yaml="yamlText" />
          </div>
          <div v-show="rightTab === 'check'" class="platform-rpane">
            <ValidationList
              :errors="allIssues.errors"
              :warnings="allIssues.warnings"
              :checked="checked"
              @locate="scrollToSection"
            />
          </div>
          <div
            v-show="rightTab === 'chat'"
            class="platform-rpane"
            style="padding: 16px"
          >
            <p class="platform-mut" style="font-size: 12.5px">
              试聊跳转到对话页并以
              <span class="platform-mono">{{ agentId }}</span> 身份开启新会话。
              注意：对话页使用的是当前已加载的运行时配置，未重启前保存的修改不会生效。
            </p>
            <button
              class="platform-btn platform-btn-pri platform-btn-sm"
              @click="router.push(`/chat?agent=${agentId}`)"
            >
              前往试聊 →
            </button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
