<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import type { AgentSummary, SkillSummary } from '../api/platformClient'
import { defaultPlatformClient } from './client'
import { showToast } from './toast'

const client = defaultPlatformClient()
const router = useRouter()

const tab = ref<'global' | 'private'>('global')
const agents = ref<AgentSummary[]>([])
const privateScope = ref('')
const globalSkills = ref<SkillSummary[] | null>(null)
const privateSkills = ref<SkillSummary[] | null>(null)
const error = ref<string | null>(null)
const loading = ref(false)
const deleting = ref<{ scope: string; skill: SkillSummary } | null>(null)
const deleteConfirmText = ref('')
const deleteBusy = ref(false)
const creating = ref(false)
const createScope = ref('global')

const currentSkills = computed(() =>
  tab.value === 'global' ? globalSkills.value : privateSkills.value,
)

async function loadAgents(): Promise<void> {
  try {
    agents.value = await client.listAgents()
    privateScope.value = privateScope.value || agents.value[0]?.agent_id || ''
  } catch (caught) {
    showToast(caught instanceof Error ? caught.message : 'Agent 列表加载失败', 'error')
  }
}

async function loadSkills(): Promise<void> {
  loading.value = true
  error.value = null
  try {
    if (tab.value === 'global') {
      const result = await client.listSkills('global')
      globalSkills.value = result.skills
    } else {
      if (!privateScope.value) {
        privateSkills.value = []
        return
      }
      const result = await client.listSkills(privateScope.value)
      privateSkills.value = result.skills
      if (result.global_skills) {
        globalSkills.value = result.global_skills
      }
    }
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : 'Skill 列表加载失败'
  } finally {
    loading.value = false
  }
}

function switchTab(next: 'global' | 'private'): void {
  tab.value = next
  void loadSkills()
}

function editPath(scope: string, name: string): string {
  return `/platform/skills/${scope}/${name}/edit`
}

function openCreate(): void {
  createScope.value = tab.value === 'global' ? 'global' : privateScope.value
  creating.value = true
}

/** 打开删除确认框；独立方法避免多语句内联 handler。 */
function startDelete(skill: SkillSummary): void {
  deleting.value = {
    scope: skill.source === 'global' ? 'global' : privateScope.value,
    skill,
  }
  deleteConfirmText.value = ''
}

function confirmCreate(): void {
  creating.value = false
  router.push(`/platform/skills/new?scope=${encodeURIComponent(createScope.value)}`)
}

async function confirmDelete(): Promise<void> {
  const target = deleting.value
  if (!target) return
  if (
    target.skill.referenced_by.length > 0 &&
    deleteConfirmText.value !== target.skill.name
  ) {
    showToast(`请输入 Skill 名称「${target.skill.name}」以二次确认`, 'error')
    return
  }
  deleteBusy.value = true
  try {
    await client.deleteSkill(target.scope, target.skill.name)
    showToast(`✓ 已删除 ${target.skill.name} · 重启后生效`)
    deleting.value = null
    deleteConfirmText.value = ''
    await loadSkills()
  } catch (caught) {
    showToast(caught instanceof Error ? caught.message : '删除失败', 'error')
  } finally {
    deleteBusy.value = false
  }
}

onMounted(async () => {
  await loadAgents()
  await loadSkills()
})
</script>

<template>
  <div class="platform-root">
    <div class="platform-page">
      <div class="platform-page-inner">
        <div class="platform-page-head">
          <div>
            <h1>Skill 仓库</h1>
            <p>通用库对所有 Agent 可见 · 私有库仅归属 Agent 可见，同名时私有覆盖通用</p>
          </div>
          <button class="platform-btn platform-btn-pri" @click="openCreate"
            >＋ 新建 Skill</button
          >
        </div>

        <div style="display: flex; gap: 10px; align-items: center; margin-bottom: 18px">
          <div class="platform-libtabs">
            <div
              class="platform-libtab"
              :class="{ on: tab === 'global' }"
              @click="switchTab('global')"
            >
              通用库 <span class="platform-mono" style="opacity: 0.7">skills/</span>
            </div>
            <div
              class="platform-libtab"
              :class="{ on: tab === 'private' }"
              @click="switchTab('private')"
              >私有库</div
            >
          </div>
          <select
            v-if="tab === 'private'"
            v-model="privateScope"
            style="max-width: 230px"
            aria-label="选择 Agent"
            @change="loadSkills"
          >
            <option
              v-for="agent in agents"
              :key="agent.agent_id"
              :value="agent.agent_id"
              >{{ agent.agent_id }}</option
            >
          </select>
        </div>

        <div v-if="error" class="platform-state">
          <p role="alert">{{ error }}</p>
          <button class="platform-btn" @click="loadSkills">重试</button>
        </div>
        <div v-else-if="loading || !currentSkills" class="platform-state"
          >正在加载 Skill…</div
        >
        <template v-else>
          <div v-if="tab === 'global'" class="platform-notice platform-notice-violet">
            ◈ 通用库的修改 / 删除 / 改名会影响所有引用它的 Agent，保存前请确认影响面
          </div>
          <div v-else class="platform-notice platform-notice-cyan">
            ◎ 当前查看 <b>{{ privateScope || '（无 Agent）' }}</b> 的私有库 · 其他 Agent
            不可见这些 Skill · 同名时覆盖通用库
          </div>

          <div v-if="!currentSkills.length" class="platform-state">暂无 Skill</div>
          <div class="platform-grid-3">
            <div
              v-for="skill in currentSkills"
              :key="skill.name"
              class="platform-card platform-skcard"
            >
              <div
                style="display: flex; gap: 8px; align-items: center; margin-bottom: 7px"
              >
                <b>{{ skill.name }}</b>
                <span
                  class="platform-badge"
                  :class="
                    skill.source === 'global' ? 'platform-b-violet' : 'platform-b-cyan'
                  "
                >
                  {{ skill.source === 'global' ? '通用' : '私有' }}
                </span>
                <span
                  v-if="!skill.valid"
                  class="platform-badge platform-b-red"
                  :title="skill.error ?? ''"
                  >无效</span
                >
                <span
                  v-if="skill.overrides_global"
                  class="platform-badge platform-b-amber"
                  >覆盖通用</span
                >
                <span
                  v-if="skill.overridden_by.length"
                  class="platform-badge platform-b-amber"
                  :title="`被 ${skill.overridden_by.join('、')} 的私有版本覆盖`"
                >
                  {{ skill.overridden_by.length }} 处被覆盖
                </span>
              </div>
              <p class="platform-mut" style="font-size: 12.5px; margin: 0 0 12px">{{
                skill.description || '（无描述）'
              }}</p>
              <div
                style="
                  display: flex;
                  align-items: center;
                  gap: 7px;
                  font-size: 11.5px;
                  flex-wrap: wrap;
                "
              >
                <span class="platform-badge platform-b-mut"
                  >{{ skill.resources.length }} 资源</span
                >
                <template v-if="skill.referenced_by.length">
                  <span class="platform-mut">被引用</span>
                  <span
                    v-for="ref in skill.referenced_by.slice(0, 2)"
                    :key="ref"
                    class="platform-badge platform-b-mut"
                    >{{ ref }}</span
                  >
                  <span
                    v-if="skill.referenced_by.length > 2"
                    class="platform-badge platform-b-mut"
                    >+{{ skill.referenced_by.length - 2 }}</span
                  >
                </template>
                <span v-else class="platform-dim">未被任何 Agent 引用</span>
              </div>
              <div class="platform-skfoot">
                <span class="platform-dim platform-mono" style="font-size: 11px">{{
                  skill.name
                }}</span>
                <span>
                  <button
                    class="platform-btn platform-btn-sm platform-btn-ghost"
                    @click="
                      router.push(
                        editPath(
                          skill.source === 'global' ? 'global' : privateScope,
                          skill.name,
                        ),
                      )
                    "
                    >编辑</button
                  >
                  <button
                    class="platform-btn platform-btn-sm platform-btn-ghost platform-btn-dngr"
                    @click="startDelete(skill)"
                    >删除</button
                  >
                </span>
              </div>
            </div>
            <div
              class="platform-card platform-newcard"
              style="min-height: 150px"
              @click="openCreate"
            >
              <div style="font-size: 13px"
                >＋ 新建{{ tab === 'global' ? '通用' : '私有' }} Skill</div
              >
            </div>
          </div>
        </template>
      </div>
    </div>

    <Teleport to="body">
      <div v-if="creating" class="platform-modal-mask" @click.self="creating = false">
        <div class="platform-modal" role="dialog" aria-label="新建 Skill">
          <h3>新建 Skill</h3>
          <p>选择 Skill 归属的库：通用库对所有 Agent 可见；私有库仅归属 Agent 可见。</p>
          <label>归属范围</label>
          <select v-model="createScope" aria-label="Skill 范围">
            <option value="global">通用库（skills/）</option>
            <option
              v-for="agent in agents"
              :key="agent.agent_id"
              :value="agent.agent_id"
            >
              私有库 · {{ agent.agent_id }}
            </option>
          </select>
          <div class="platform-modal-actions">
            <button class="platform-btn platform-btn-sm" @click="creating = false"
              >取消</button
            >
            <button
              class="platform-btn platform-btn-sm platform-btn-pri"
              @click="confirmCreate"
              >继续 →</button
            >
          </div>
        </div>
      </div>

      <div v-if="deleting" class="platform-modal-mask" @click.self="deleting = null">
        <div class="platform-modal" role="dialog" aria-label="删除 Skill">
          <h3>删除 Skill「{{ deleting.skill.name }}」</h3>
          <template v-if="deleting.skill.referenced_by.length">
            <p
              >以下 {{ deleting.skill.referenced_by.length }} 个 Agent 引用了该
              Skill，删除后它们将加载失败或降级：</p
            >
            <div style="display: flex; gap: 7px; flex-wrap: wrap; margin-bottom: 14px">
              <span
                v-for="ref in deleting.skill.referenced_by"
                :key="ref"
                class="platform-badge platform-b-red"
                >{{ ref }}</span
              >
            </div>
            <label>输入 Skill 名称「{{ deleting.skill.name }}」以确认删除</label>
            <input
              v-model="deleteConfirmText"
              type="text"
              class="platform-mono"
              aria-label="确认删除输入"
            />
          </template>
          <p v-else>没有 Agent 引用该 Skill，删除后重启生效。</p>
          <div class="platform-modal-actions">
            <button
              class="platform-btn platform-btn-sm"
              :disabled="deleteBusy"
              @click="deleting = null"
              >取消</button
            >
            <button
              class="platform-btn platform-btn-sm platform-btn-dngr"
              :disabled="deleteBusy"
              @click="confirmDelete"
            >
              {{ deleteBusy ? '删除中…' : '确认删除' }}
            </button>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>
