<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import type { SkillResource } from '../api/platformClient'
import { defaultPlatformClient } from './client'
import { showToast } from './toast'

const route = useRoute()
const router = useRouter()
const client = defaultPlatformClient()

const SKILL_NAME_RE = /^[a-z0-9][a-z0-9-]*$/

const isCreate = computed(() => route.name === 'platform-skill-new')
const scope = ref(
  isCreate.value
    ? typeof route.query.scope === 'string' && route.query.scope
      ? route.query.scope
      : 'global'
    : String(route.params.scope),
)
const originalName = ref(isCreate.value ? '' : String(route.params.name))

const name = ref(originalName.value)
const description = ref('')
const metadataPairs = ref<{ key: string; value: string }[]>([])
const allowedTools = ref<string[]>([])
const toolInput = ref('')
const content = ref('')
const previewing = ref(false)
const resources = ref<SkillResource[]>([])
const loadError = ref<string | null>(null)
const loaded = ref(isCreate.value)
const saving = ref(false)
const resourceError = ref<string | null>(null)
const resourceDraft = ref<{
  path: string
  content: string
  isNew: boolean
} | null>(null)

const charCount = computed(() => content.value.length)

/** 顶部展示的 SKILL.md 目标路径预览；占位符避免在模板中直接写尖括号。 */
const skillPathPreview = computed(() => {
  const placeholder = name.value || '<name>'
  return scope.value === 'global'
    ? `skills/${placeholder}/SKILL.md`
    : `application/${scope.value}/skills/${placeholder}/SKILL.md`
})

const nameError = computed(() => {
  if (!name.value.trim()) return 'name 不能为空'
  if (!SKILL_NAME_RE.test(name.value.trim())) {
    return 'name 只能包含小写字母、数字和连字符，且以字母或数字开头'
  }
  return null
})

function validResourcePath(path: string): string | null {
  const trimmed = path.trim()
  if (!trimmed) return '路径不能为空'
  if (trimmed.startsWith('/') || trimmed.includes('..') || trimmed.includes('\\')) {
    return '路径必须是 Skill 目录内的相对路径，禁止 .. 与绝对路径'
  }
  return null
}

async function load(): Promise<void> {
  loadError.value = null
  try {
    const [detail, resourceList] = await Promise.all([
      client.getSkill(scope.value, originalName.value),
      client.listSkillResources(scope.value, originalName.value).catch(() => []),
    ])
    description.value = detail.description
    content.value = detail.content
    allowedTools.value = [...detail.allowed_tools]
    metadataPairs.value = Object.entries(detail.metadata).map(([key, value]) => ({
      key,
      value,
    }))
    resources.value = resourceList
    loaded.value = true
  } catch (caught) {
    loadError.value = caught instanceof Error ? caught.message : 'Skill 加载失败'
  }
}

function metadataObject(): Record<string, string> {
  const result: Record<string, string> = {}
  for (const pair of metadataPairs.value) {
    if (pair.key.trim()) result[pair.key.trim()] = pair.value
  }
  return result
}

function addTool(): void {
  const value = toolInput.value.trim()
  if (value && !allowedTools.value.includes(value)) allowedTools.value.push(value)
  toolInput.value = ''
}

async function save(): Promise<void> {
  if (nameError.value) {
    showToast(nameError.value, 'error')
    return
  }
  if (!description.value.trim()) {
    showToast('description 为必填项', 'error')
    return
  }
  saving.value = true
  try {
    if (isCreate.value) {
      const skill = await client.createSkill({
        scope: scope.value,
        name: name.value.trim(),
        description: description.value,
        content: content.value,
        metadata: metadataObject(),
        allowed_tools: allowedTools.value,
      })
      showToast(`✓ 已创建 ${skill.name} · 重启后生效`)
      router.replace(`/platform/skills/${scope.value}/${skill.name}/edit`)
    } else {
      await client.updateSkill(scope.value, originalName.value, {
        description: description.value,
        content: content.value,
        metadata: metadataObject(),
        allowed_tools: allowedTools.value,
      })
      showToast('✓ 已写入 SKILL.md · 重启后生效')
    }
  } catch (caught) {
    showToast(caught instanceof Error ? caught.message : '保存失败', 'error')
  } finally {
    saving.value = false
  }
}

async function refreshResources(): Promise<void> {
  try {
    resources.value = await client.listSkillResources(scope.value, originalName.value)
  } catch (caught) {
    showToast(caught instanceof Error ? caught.message : '资源列表刷新失败', 'error')
  }
}

async function removeResource(path: string): Promise<void> {
  try {
    await client.deleteSkillResource(scope.value, originalName.value, path)
    showToast(`✓ 已删除 ${path}`)
    await refreshResources()
  } catch (caught) {
    showToast(caught instanceof Error ? caught.message : '删除资源失败', 'error')
  }
}

async function saveResource(): Promise<void> {
  const target = resourceDraft.value
  if (!target) return
  const invalid = validResourcePath(target.path)
  resourceError.value = invalid
  if (invalid) return
  try {
    await client.putSkillResource(
      scope.value,
      originalName.value,
      target.path.trim(),
      target.content,
    )
    showToast(`✓ 已保存 ${target.path}`)
    resourceDraft.value = null
    resourceError.value = null
    await refreshResources()
  } catch (caught) {
    showToast(caught instanceof Error ? caught.message : '保存资源失败', 'error')
  }
}

async function openResource(path: string): Promise<void> {
  resourceError.value = null
  resourceDraft.value = { path, content: '（加载中…）', isNew: false }
  try {
    const existing = await client.getSkillResource(
      scope.value,
      originalName.value,
      path,
    )
    resourceDraft.value = { path, content: existing, isNew: false }
  } catch {
    resourceDraft.value = {
      path,
      content: '',
      isNew: false,
    }
    resourceError.value = '未能读取现有内容（后端可能未实现资源读取），保存将覆盖该文件'
  }
}

/** 打开新建资源草稿；独立方法避免多语句内联 handler。 */
function startNewResource(): void {
  resourceDraft.value = { path: '', content: '', isNew: true }
  resourceError.value = null
}

function formatSize(size: number): string {
  return size >= 1024 ? `${(size / 1024).toFixed(1)} KB` : `${size} B`
}

onMounted(() => {
  if (!isCreate.value) void load()
})
</script>

<template>
  <div class="platform-root">
    <div v-if="loadError" class="platform-state">
      <p role="alert">{{ loadError }}</p>
      <button class="platform-btn" @click="load">重试</button>
    </div>
    <div v-else-if="!loaded" class="platform-state">正在加载 Skill…</div>
    <div v-else class="platform-editor">
      <div class="platform-editor-topbar">
        <button
          class="platform-btn platform-btn-ghost platform-btn-sm"
          @click="router.push('/platform/skills')"
          >← 返回仓库</button
        >
        <b>{{ name || '新建 Skill' }}</b>
        <span
          class="platform-badge"
          :class="scope === 'global' ? 'platform-b-violet' : 'platform-b-cyan'"
        >
          {{ scope === 'global' ? '通用' : `私有 · ${scope}` }}
        </span>
        <span class="platform-mono platform-dim" style="font-size: 11px">
          {{ skillPathPreview }}
        </span>
        <div class="platform-editor-topbar-actions">
          <button
            class="platform-btn platform-btn-sm platform-btn-pri"
            :disabled="saving"
            @click="save"
          >
            {{ saving ? '保存中…' : '保存（重启后生效）' }}
          </button>
        </div>
      </div>
      <div class="platform-editor-grid platform-editor-skill">
        <!-- 左：frontmatter -->
        <div class="platform-editor-side" style="padding: 20px">
          <div class="platform-side-label" style="padding: 0 0 12px">FRONTMATTER</div>
          <div class="platform-field">
            <label>
              name
              <span
                v-if="!isCreate"
                class="platform-badge platform-b-mut"
                style="margin-left: 4px"
                >创建后锁定</span
              >
            </label>
            <input
              v-model="name"
              type="text"
              class="platform-mono"
              :disabled="!isCreate"
              placeholder="fraud-playbook"
              aria-label="skill name"
            />
            <div v-if="isCreate && name && nameError" class="platform-hint"
              >⚠ {{ nameError }}</div
            >
            <div v-else class="platform-hint" style="color: var(--p-mut)"
              >须为 kebab-case，且与目录名一致</div
            >
          </div>
          <div class="platform-field">
            <label
              >description
              <span class="platform-badge platform-b-red" style="margin-left: 4px"
                >必填</span
              ></label
            >
            <textarea
              v-model="description"
              rows="3"
              aria-label="skill description"
              placeholder="该描述会进入 SystemPrompt 的 Skill 目录"
            />
          </div>
          <div class="platform-field">
            <label
              >allowed-tools
              <span class="platform-mut" style="font-weight: 400"
                >· 仅声明，不参与授权</span
              ></label
            >
            <div class="platform-capbox">
              <div
                v-for="(tool, index) in allowedTools"
                :key="tool"
                class="platform-captag"
              >
                {{ tool }}<i @click="allowedTools.splice(index, 1)">×</i>
              </div>
            </div>
            <div style="display: flex; gap: 8px; margin-top: 8px">
              <input
                v-model="toolInput"
                type="text"
                class="platform-mono"
                placeholder="工具名，回车添加"
                aria-label="添加 allowed tool"
                @keydown.enter.prevent="addTool"
              />
              <button class="platform-btn platform-btn-sm" @click="addTool">＋</button>
            </div>
            <div class="platform-hint" style="color: var(--p-mut)"
              >按项目规范：不解析、不校验、不影响工具依赖</div
            >
          </div>
          <div class="platform-field">
            <label>metadata</label>
            <div
              v-for="(pair, index) in metadataPairs"
              :key="index"
              class="platform-kvrow"
            >
              <input
                v-model="pair.key"
                type="text"
                class="platform-mono"
                placeholder="key"
                aria-label="metadata key"
              />
              <input
                v-model="pair.value"
                type="text"
                class="platform-mono"
                placeholder="value"
                aria-label="metadata value"
              />
              <i
                style="cursor: pointer; color: var(--p-dim); font-style: normal"
                @click="metadataPairs.splice(index, 1)"
                >×</i
              >
            </div>
            <span
              class="platform-badge platform-b-mut"
              style="cursor: pointer"
              @click="metadataPairs.push({ key: '', value: '' })"
              >＋ 添加键值</span
            >
          </div>
        </div>

        <!-- 中：SKILL.md -->
        <div
          style="
            overflow-y: auto;
            padding: 20px 24px;
            display: flex;
            flex-direction: column;
            min-height: 0;
          "
        >
          <div style="display: flex; gap: 6px; margin-bottom: 10px; flex: none">
            <span
              class="platform-badge"
              :class="previewing ? 'platform-b-mut' : 'platform-b-cyan'"
              style="cursor: pointer"
              @click="previewing = false"
              >编辑</span
            >
            <span
              class="platform-badge"
              :class="previewing ? 'platform-b-cyan' : 'platform-b-mut'"
              style="cursor: pointer"
              @click="previewing = true"
              >预览</span
            >
            <span
              class="platform-mut"
              style="margin-left: auto; font-size: 11.5px; align-self: center"
              >SKILL.md · {{ charCount }} 字符</span
            >
          </div>
          <textarea
            v-if="!previewing"
            v-model="content"
            class="platform-mono"
            style="flex: 1; min-height: 400px; line-height: 1.75; font-size: 12.5px"
            aria-label="SKILL.md 内容"
          />
          <pre
            v-else
            class="platform-yaml platform-card"
            style="flex: 1; overflow-y: auto"
            >{{ content || '（空）' }}</pre
          >
        </div>

        <!-- 右：资源文件 -->
        <div
          class="platform-editor-right"
          style="border-right: none; padding: 20px; overflow-y: auto"
        >
          <div class="platform-side-label" style="padding: 0 0 12px">关联资源文件</div>
          <template v-if="isCreate">
            <p class="platform-mut" style="font-size: 12.5px"
              >资源文件在 Skill 创建后可管理。</p
            >
          </template>
          <template v-else>
            <div
              v-for="resource in resources"
              :key="resource.path"
              class="platform-resrow"
            >
              <span
                class="platform-mono"
                style="cursor: pointer"
                @click="openResource(resource.path)"
                >{{ resource.path }}</span
              >
              <span class="platform-dim">{{ formatSize(resource.size) }}</span>
              <i title="删除" @click="removeResource(resource.path)">×</i>
            </div>
            <div
              v-if="!resources.length"
              class="platform-dim"
              style="font-size: 12.5px; margin-bottom: 10px"
              >暂无资源文件</div
            >
            <button
              class="platform-btn platform-btn-sm"
              style="width: 100%; justify-content: center; margin: 10px 0 22px"
              @click="startNewResource"
            >
              ＋ 新建文本文件
            </button>
          </template>
        </div>
      </div>
    </div>

    <Teleport to="body">
      <div
        v-if="resourceDraft"
        class="platform-modal-mask"
        @click.self="resourceDraft = null"
      >
        <div class="platform-modal" role="dialog" aria-label="编辑资源">
          <h3>{{ resourceDraft.isNew ? '新建文本文件' : '编辑资源' }}</h3>
          <label>路径（Skill 目录内相对路径）</label>
          <input
            v-model="resourceDraft.path"
            type="text"
            class="platform-mono"
            :disabled="!resourceDraft.isNew"
            placeholder="references/escalation.md"
            aria-label="资源路径"
          />
          <div v-if="resourceError" class="platform-hint" style="margin: 6px 0"
            >⚠ {{ resourceError }}</div
          >
          <label style="margin-top: 12px">内容</label>
          <textarea
            v-model="resourceDraft.content"
            rows="8"
            class="platform-mono"
            aria-label="资源内容"
          />
          <div class="platform-modal-actions">
            <button class="platform-btn platform-btn-sm" @click="resourceDraft = null"
              >取消</button
            >
            <button
              class="platform-btn platform-btn-sm platform-btn-pri"
              @click="saveResource"
              >保存</button
            >
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>
