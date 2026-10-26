<script setup lang="ts">
import { computed, ref } from 'vue'

import type { Catalog, CatalogItem } from '../../api/platformClient'
import type { AgentFormState } from '../useAgentForm'

const props = defineProps<{
  form: AgentFormState
  catalog: Catalog | null
  /** 当前 Agent 的私有 Skill 名（编辑页从 skills API 获取）。 */
  privateSkillNames?: string[]
  /**
   * 当前正在编辑的 Agent id。能力可选项 = builtin（agent_id 为空）+ 归属该
   * Agent 的私有插件；不传（新建向导）时新 Agent 尚无私有插件，只显示 builtin。
   */
  currentAgentId?: string
}>()

/** 判断 catalog 项是否对本 Agent 可用。 */
function isAllowed(item: CatalogItem): boolean {
  return item.agent_id == null || item.agent_id === props.currentAgentId
}

const toolFilter = ref('')

const globalSkillNames = computed(
  () => new Set((props.catalog?.global_skills ?? []).map((skill) => skill.name)),
)
const privateSkillSet = computed(() => new Set(props.privateSkillNames ?? []))

const skillOptions = computed(() => {
  const selected = new Set(props.form.enabledSkills)
  const options: { name: string; kind: 'global' | 'private' }[] = []
  for (const skill of props.catalog?.global_skills ?? []) {
    if (!selected.has(skill.name)) options.push({ name: skill.name, kind: 'global' })
  }
  for (const name of props.privateSkillNames ?? []) {
    if (!selected.has(name) && !globalSkillNames.value.has(name)) {
      options.push({ name, kind: 'private' })
    }
  }
  return options
})

const overriddenSkills = computed(() =>
  props.form.enabledSkills.filter(
    (name) => globalSkillNames.value.has(name) && privateSkillSet.value.has(name),
  ),
)

function skillBadge(name: string): { label: string; cls: string } {
  if (privateSkillSet.value.has(name)) {
    return { label: '私有', cls: 'platform-b-cyan' }
  }
  if (globalSkillNames.value.has(name)) {
    return { label: '通用', cls: 'platform-b-violet' }
  }
  return { label: '未知', cls: 'platform-b-mut' }
}

function addSkill(event: Event): void {
  const select = event.target as HTMLSelectElement
  const name = select.value
  if (name && !props.form.enabledSkills.includes(name)) {
    props.form.enabledSkills.push(name)
  }
  select.value = ''
}

function removeSkill(index: number): void {
  props.form.enabledSkills.splice(index, 1)
}

/** 全部已注册工具（含其他 Agent 私有），用于归属查询与不可用判定。 */
const allTools = computed(() => {
  const map = new Map<string, CatalogItem>()
  for (const tool of props.catalog?.tools ?? []) map.set(tool.name, tool)
  return map
})

/** 本 Agent 可选工具：builtin + 本 Agent 私有。 */
const knownTools = computed(() =>
  (props.catalog?.tools ?? []).filter((tool) => isAllowed(tool)),
)

/** 已选但不在本 Agent 可用范围的工具（含未知项与跨 Agent 私有引用）。 */
const unavailableTools = computed(() =>
  props.form.enabledTools.filter((name) => {
    const item = allTools.value.get(name)
    return !item || !isAllowed(item)
  }),
)

/** 不可用工具的归属说明（跨 Agent 私有 / 未注册）。 */
function unavailableReason(name: string): string {
  const item = allTools.value.get(name)
  return item?.agent_id
    ? `属于 ${item.agent_id} 的私有插件`
    : '未在 Plugin 注册表中找到'
}

const visibleTools = computed(() => {
  const keyword = toolFilter.value.trim().toLowerCase()
  const selected = new Set(props.form.enabledTools)
  const rows: { name: string; source: string; checked: boolean }[] = []
  for (const tool of knownTools.value) {
    if (keyword && !tool.name.toLowerCase().includes(keyword)) continue
    rows.push({
      name: tool.name,
      source: tool.source,
      checked: selected.has(tool.name),
    })
  }
  return rows
})

function toggleTool(name: string, checked: boolean): void {
  const list = props.form.enabledTools
  const index = list.indexOf(name)
  if (checked && index < 0) list.push(name)
  if (!checked && index >= 0) list.splice(index, 1)
}

/** middleware / context_provider 可选项同样按归属过滤。 */
function allowedItems(items: CatalogItem[] | undefined): CatalogItem[] {
  return (items ?? []).filter((item) => isAllowed(item))
}

function addNamed(list: string[], event: Event): void {
  const select = event.target as HTMLSelectElement
  const name = select.value
  if (name && !list.includes(name)) list.push(name)
  select.value = ''
}
</script>

<template>
  <div class="platform-fsec" id="sec-cap">
    <h3>能力</h3>

    <div class="platform-field">
      <label
        >enabled_skills
        <span class="platform-mut" style="font-weight: 400">· 顺序有语义</span></label
      >
      <div class="platform-capbox">
        <div
          v-for="(name, index) in form.enabledSkills"
          :key="name"
          class="platform-captag"
        >
          {{ name }}
          <span class="platform-badge" :class="skillBadge(name).cls">{{
            skillBadge(name).label
          }}</span>
          <span
            v-if="overriddenSkills.includes(name)"
            class="platform-badge platform-b-amber"
            title="私有 Skill 覆盖通用库同名 Skill"
            >覆盖通用</span
          >
          <i @click="removeSkill(index)">×</i>
        </div>
        <select
          v-if="skillOptions.length"
          style="
            width: auto;
            min-width: 130px;
            padding: 3px 26px 3px 9px;
            font-size: 12px;
          "
          aria-label="添加 Skill"
          @change="addSkill"
        >
          <option value="">＋ 添加 Skill</option>
          <option
            v-for="option in skillOptions"
            :key="option.kind + option.name"
            :value="option.name"
          >
            {{ option.name }}（{{ option.kind === 'global' ? '通用' : '私有' }}）
          </option>
        </select>
        <span
          v-else-if="!form.enabledSkills.length"
          class="platform-dim"
          style="font-size: 12.5px; align-self: center"
        >
          无可选 Skill
        </span>
      </div>
      <div v-for="name in overriddenSkills" :key="name" class="platform-hint">
        ⚠
        <span class="platform-mono">{{ name }}</span>
        与通用库同名，私有版本将生效（首个同名获胜）
      </div>
    </div>

    <div class="platform-field">
      <label
        >enabled_tools
        <span class="platform-mut" style="font-weight: 400"
          >· 可选范围：builtin + 本 Agent 私有插件</span
        ></label
      >
      <div
        v-if="unavailableTools.length"
        class="platform-chk platform-chk-warn"
        style="margin-bottom: 10px"
      >
        <b>⚠ {{ unavailableTools.length }} 个已选工具不在本 Agent 可用范围</b>
        <span>
          这些工具属于模板/其他 Agent 的私有插件或未注册，保存后运行时装配会失败；
          请移除，或创建后自行实现对应 plugins。
        </span>
        <div style="display: flex; gap: 7px; flex-wrap: wrap; margin-top: 8px">
          <span
            v-for="name in unavailableTools"
            :key="name"
            class="platform-captag"
            :title="unavailableReason(name)"
          >
            {{ name }}
            <span class="platform-badge platform-b-amber">{{
              unavailableReason(name)
            }}</span>
            <i @click="toggleTool(name, false)">×</i>
          </span>
        </div>
      </div>
      <div style="display: flex; gap: 8px; margin-bottom: 8px">
        <input
          v-model="toolFilter"
          type="text"
          placeholder="搜索工具…"
          style="max-width: 220px"
          aria-label="搜索工具"
        />
        <span class="platform-mut" style="font-size: 12px; align-self: center">
          已选 {{ form.enabledTools.length }} / {{ knownTools.length }}
        </span>
      </div>
      <div class="platform-capbox" style="max-height: 190px; overflow-y: auto">
        <label v-for="row in visibleTools" :key="row.name" class="platform-toolrow">
          <input
            type="checkbox"
            :checked="row.checked"
            @change="toggleTool(row.name, ($event.target as HTMLInputElement).checked)"
          />
          <span class="platform-mono" :class="{ 'platform-dim': !row.checked }">{{
            row.name
          }}</span>
          <span
            class="platform-badge"
            :class="row.source === 'application' ? 'platform-b-cyan' : 'platform-b-mut'"
            >{{ row.source }}</span
          >
        </label>
        <span v-if="!visibleTools.length" class="platform-dim" style="font-size: 12.5px"
          >无匹配工具</span
        >
      </div>
    </div>

    <div class="platform-frow2">
      <div class="platform-field">
        <label>enabled_middlewares</label>
        <div class="platform-capbox">
          <div
            v-for="(name, index) in form.enabledMiddlewares"
            :key="name"
            class="platform-captag"
          >
            {{ name }}
            <span class="platform-badge platform-b-mut">
              {{
                (catalog?.middlewares ?? []).find((m) => m.name === name)?.source ??
                'builtin'
              }}
            </span>
            <i @click="form.enabledMiddlewares.splice(index, 1)">×</i>
          </div>
          <select
            v-if="allowedItems(catalog?.middlewares).length"
            style="
              width: auto;
              min-width: 100px;
              padding: 3px 26px 3px 9px;
              font-size: 12px;
            "
            aria-label="添加 Middleware"
            @change="addNamed(form.enabledMiddlewares, $event)"
          >
            <option value="">＋ 添加</option>
            <option
              v-for="item in allowedItems(catalog?.middlewares).filter(
                (m) => !form.enabledMiddlewares.includes(m.name),
              )"
              :key="item.name"
              :value="item.name"
              >{{ item.name }}</option
            >
          </select>
        </div>
      </div>
      <div class="platform-field">
        <label>enabled_context_providers</label>
        <div class="platform-capbox">
          <div
            v-for="(name, index) in form.enabledContextProviders"
            :key="name"
            class="platform-captag"
          >
            {{ name }}<i @click="form.enabledContextProviders.splice(index, 1)">×</i>
          </div>
          <span
            v-if="
              !form.enabledContextProviders.length &&
              !allowedItems(catalog?.context_providers).length
            "
            class="platform-dim"
            style="font-size: 12.5px; align-self: center"
            >未启用 — 默认全部关闭</span
          >
          <select
            v-if="allowedItems(catalog?.context_providers).length"
            style="
              width: auto;
              min-width: 100px;
              padding: 3px 26px 3px 9px;
              font-size: 12px;
            "
            aria-label="添加 ContextProvider"
            @change="addNamed(form.enabledContextProviders, $event)"
          >
            <option value="">＋ 添加</option>
            <option
              v-for="item in allowedItems(catalog?.context_providers).filter(
                (m) => !form.enabledContextProviders.includes(m.name),
              )"
              :key="item.name"
              :value="item.name"
              >{{ item.name }}</option
            >
          </select>
        </div>
      </div>
    </div>
  </div>
</template>
