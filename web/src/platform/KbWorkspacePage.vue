<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'

import { RuntimeHttpError } from '../api/client'
import {
  defaultKbClient,
  type KbDocument,
  type KbImportPreview,
  type KbImportPreviewChunk,
  type KbManifest,
  type KbSummary,
} from './kbClient'
import { showToast } from './toast'

const route = useRoute()
const client = defaultKbClient()

const scope = String(route.params.scope)
const name = String(route.params.name)

const summary = ref<KbSummary | null>(null)
const docs = ref<KbDocument[] | null>(null)
const loadError = ref<string | null>(null)
const filter = ref('')
const selectedId = ref<string | null>(null)
const creatingNew = ref(false)
const saving = ref(false)
const deleting = ref(false)
const editor = reactive({ doc_id: '', title: '', category: '', content: '' })
const settingsOpen = ref(false)
const settingsText = ref('')
const settingsError = ref<string | null>(null)
const settingsBusy = ref(false)

/** 上传导入弹窗中的一行候选块：预览 chunk + 勾选态与可编辑字段。 */
interface ImportRow {
  chunk: KbImportPreviewChunk
  selected: boolean
  title: string
  category: string
}

const importOpen = ref(false)
const importStep = ref<'pick' | 'preview'>('pick')
const importLoading = ref(false)
const importSubmitting = ref(false)
const importError = ref<string | null>(null)
const importPreviewData = ref<KbImportPreview | null>(null)
const importRows = ref<ImportRow[]>([])

const filteredDocs = computed(() => {
  const keyword = filter.value.trim().toLowerCase()
  const list = docs.value ?? []
  if (!keyword) return list
  return list.filter(
    (doc) =>
      doc.doc_id.toLowerCase().includes(keyword) ||
      doc.title.toLowerCase().includes(keyword),
  )
})

const summaryLine = computed(() => {
  const kb = summary.value
  if (!kb || !kb.valid) return ''
  const parts = [`默认策略 ${kb.strategy ?? '—'}`]
  parts.push(kb.has_gate ? '裁判已启用' : '裁判未启用')
  if (kb.has_rerank) parts.push('rerank 已启用')
  return parts.join(' · ')
})

async function load(): Promise<void> {
  loadError.value = null
  try {
    const [summaries, docList] = await Promise.all([
      client.listBases(),
      client.listDocs(scope, name),
    ])
    summary.value =
      summaries.find((kb) => kb.scope === scope && kb.name === name) ?? null
    docs.value = docList
    const first = docList[0]
    if (first && !selectedId.value) selectDoc(first)
  } catch (caught) {
    loadError.value = caught instanceof Error ? caught.message : '知识库加载失败'
  }
}

function selectDoc(doc: KbDocument): void {
  selectedId.value = doc.doc_id
  creatingNew.value = false
  editor.doc_id = doc.doc_id
  editor.title = doc.title
  editor.category = doc.category
  editor.content = doc.content
}

function startCreate(): void {
  selectedId.value = null
  creatingNew.value = true
  editor.doc_id = ''
  editor.title = ''
  editor.category = ''
  editor.content = ''
}

function today(): string {
  return new Date().toISOString().slice(0, 10)
}

async function save(): Promise<void> {
  const doc: KbDocument = {
    doc_id: editor.doc_id.trim(),
    title: editor.title.trim(),
    category: editor.category.trim(),
    content: editor.content,
    updated_at: today(),
  }
  if (!doc.doc_id || !doc.title || !doc.category || !doc.content.trim()) {
    showToast('doc_id / 标题 / 分类 / 内容均不能为空', 'error')
    return
  }
  saving.value = true
  try {
    if (creatingNew.value) {
      await client.createDoc(scope, name, doc)
      showToast(`✓ 已新增 ${doc.doc_id}`)
    } else {
      await client.updateDoc(scope, name, doc)
      showToast(`✓ 已保存 ${doc.doc_id}`)
    }
    creatingNew.value = false
    await load()
    selectDoc(doc)
  } catch (caught) {
    showToast(caught instanceof Error ? caught.message : '保存失败', 'error')
  } finally {
    saving.value = false
  }
}

async function remove(): Promise<void> {
  const docId = selectedId.value
  if (!docId || deleting.value) return
  deleting.value = true
  try {
    await client.deleteDoc(scope, name, docId)
    showToast(`✓ 已删除 ${docId}`)
    selectedId.value = null
    await load()
    const first = docs.value?.[0]
    if (first) selectDoc(first)
    else startCreate()
  } catch (caught) {
    showToast(caught instanceof Error ? caught.message : '删除失败', 'error')
  } finally {
    deleting.value = false
  }
}

const importSelectedCount = computed(
  () => importRows.value.filter((row) => row.selected).length,
)

function openImport(): void {
  importError.value = null
  importPreviewData.value = null
  importRows.value = []
  importStep.value = 'pick'
  importOpen.value = true
}

function closeImport(): void {
  if (importSubmitting.value) return
  importOpen.value = false
}

async function onImportFileChange(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  // 清空 value，允许失败后重选同一个文件再次触发 change。
  input.value = ''
  if (!file) return
  importError.value = null
  importLoading.value = true
  try {
    const preview = await client.importPreview(scope, name, file)
    importPreviewData.value = preview
    importRows.value = preview.chunks.map((chunk) => ({
      chunk,
      selected: chunk.duplicate_of === null,
      title: chunk.suggested_title,
      category: chunk.suggested_category,
    }))
    importStep.value = 'preview'
  } catch (caught) {
    importError.value = caught instanceof Error ? caught.message : '解析文件失败'
    importStep.value = 'pick'
  } finally {
    importLoading.value = false
  }
}

async function confirmImport(): Promise<void> {
  const rows = importRows.value.filter((row) => row.selected)
  if (!rows.length || importSubmitting.value) return
  const importDocsBody: KbDocument[] = rows.map((row) => ({
    doc_id: row.chunk.suggested_doc_id,
    title: row.title.trim() || row.chunk.suggested_title,
    category: row.category.trim() || row.chunk.suggested_category,
    content: row.chunk.content,
    updated_at: today(),
  }))
  importSubmitting.value = true
  try {
    await client.importDocs(scope, name, importDocsBody)
    showToast(`✓ 已导入 ${importDocsBody.length} 篇文档`)
    importOpen.value = false
    await load()
  } catch (caught) {
    if (caught instanceof RuntimeHttpError && caught.status === 409) {
      showToast(`导入冲突：${caught.message}`, 'error')
    } else {
      showToast(caught instanceof Error ? caught.message : '导入失败', 'error')
    }
  } finally {
    importSubmitting.value = false
  }
}

async function openSettings(): Promise<void> {
  settingsError.value = null
  settingsOpen.value = true
  try {
    const manifest: KbManifest = await client.getManifest(scope, name)
    settingsText.value = JSON.stringify(manifest.config, null, 2)
  } catch (caught) {
    settingsError.value = caught instanceof Error ? caught.message : 'kb.yaml 加载失败'
  }
}

async function saveSettings(): Promise<void> {
  let config: Record<string, unknown>
  try {
    const parsed: unknown = JSON.parse(settingsText.value)
    if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
      settingsError.value = '配置必须是 JSON 对象'
      return
    }
    config = parsed as Record<string, unknown>
  } catch {
    settingsError.value = '不是合法 JSON'
    return
  }
  settingsBusy.value = true
  settingsError.value = null
  try {
    await client.updateManifest(scope, name, config)
    showToast('✓ kb.yaml 已保存 · 下次检索生效')
    settingsOpen.value = false
    await load()
  } catch (caught) {
    settingsError.value = caught instanceof Error ? caught.message : '保存失败'
  } finally {
    settingsBusy.value = false
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
    <template v-else>
      <div class="kb-crumb">
        <RouterLink to="/platform/knowledge" class="kb-crumb-link">← 知识库</RouterLink>
        <b>{{ name }}</b>
        <span
          class="platform-badge"
          :class="scope === 'global' ? 'platform-b-green' : 'platform-b-cyan'"
          >{{ scope === 'global' ? '全局' : `${scope} 应用` }}</span
        >
        <span class="platform-mut" style="font-size: 12px">{{ summaryLine }}</span>
        <button
          class="platform-btn platform-btn-ghost platform-btn-sm"
          @click="openSettings"
          >库设置</button
        >
        <span style="margin-left: auto"></span>
        <RouterLink
          :to="`/platform/knowledge/${scope}/${name}/test?tab=single`"
          class="platform-btn platform-btn-ghost platform-btn-sm"
          >单测</RouterLink
        >
        <RouterLink
          :to="`/platform/knowledge/${scope}/${name}/test?tab=batch`"
          class="platform-btn platform-btn-ghost platform-btn-sm"
          >批测</RouterLink
        >
      </div>

      <div class="kb-ws-body">
        <section class="kb-ws-docs">
          <div class="kb-ws-toolbar">
            <input
              v-model="filter"
              type="text"
              placeholder="按 doc_id / 标题过滤"
              aria-label="过滤文档"
            />
            <button
              class="platform-btn platform-btn-sm platform-btn-pri"
              @click="startCreate"
              >＋ 新增文档</button
            >
            <button
              class="platform-btn platform-btn-sm platform-btn-ghost"
              @click="openImport"
              >⇪ 上传导入</button
            >
          </div>
          <div v-if="!docs" class="platform-state">正在加载文档…</div>
          <div v-else-if="!filteredDocs.length" class="platform-state">
            {{ docs.length ? '无匹配文档' : '暂无文档，点击「新增文档」开始' }}
          </div>
          <table v-else class="kb-doc-table">
            <thead>
              <tr><th>doc_id</th><th>标题</th><th>分类</th><th>更新时间</th></tr>
            </thead>
            <tbody>
              <tr
                v-for="doc in filteredDocs"
                :key="doc.doc_id"
                :class="{ selected: doc.doc_id === selectedId && !creatingNew }"
                @click="selectDoc(doc)"
              >
                <td class="platform-mono">{{ doc.doc_id }}</td>
                <td>{{ doc.title }}</td>
                <td>{{ doc.category }}</td>
                <td class="platform-mut">{{ doc.updated_at }}</td>
              </tr>
            </tbody>
          </table>
        </section>

        <section class="kb-ws-editor">
          <div class="platform-field">
            <label>doc_id</label>
            <input
              v-model="editor.doc_id"
              type="text"
              class="platform-mono"
              :disabled="!creatingNew"
              placeholder="pricing-007"
              aria-label="doc_id"
            />
          </div>
          <div class="platform-field">
            <label>标题</label>
            <input v-model="editor.title" type="text" aria-label="标题" />
          </div>
          <div class="platform-field">
            <label>分类</label>
            <input v-model="editor.category" type="text" aria-label="分类" />
          </div>
          <div class="platform-field">
            <label>内容</label>
            <textarea v-model="editor.content" rows="14" aria-label="内容"></textarea>
          </div>
          <div style="display: flex; gap: 8px; align-items: center">
            <button
              class="platform-btn platform-btn-pri platform-btn-sm"
              :disabled="saving"
              @click="save"
            >
              {{ saving ? '保存中…' : creatingNew ? '创建' : '保存' }}
            </button>
            <button
              v-if="!creatingNew && selectedId"
              class="platform-btn platform-btn-ghost platform-btn-sm platform-btn-dngr"
              :disabled="deleting"
              @click="remove"
            >
              {{ deleting ? '删除中…' : '删除' }}
            </button>
            <span class="platform-dim" style="font-size: 12px"
              >保存即生效（下次检索自动重建索引）</span
            >
          </div>
        </section>
      </div>
    </template>

    <Teleport to="body">
      <div
        v-if="settingsOpen"
        class="platform-modal-mask"
        @click.self="settingsOpen = false"
      >
        <div
          class="platform-modal"
          role="dialog"
          aria-label="库设置"
          style="width: min(680px, calc(100vw - 48px))"
        >
          <h3>库设置 · {{ name }}</h3>
          <p>
            kb.yaml 原文以 JSON 形式编辑（YAML 兼容 JSON 子集）；${VAR}
            占位符会原样保留，密钥不会被回显或落盘。name 不可修改。
          </p>
          <p v-if="settingsError" role="alert" style="color: var(--p-red)">
            ✕ {{ settingsError }}
          </p>
          <textarea
            v-model="settingsText"
            rows="16"
            class="platform-mono"
            aria-label="kb.yaml 内容"
            spellcheck="false"
          ></textarea>
          <div class="platform-modal-actions">
            <button
              class="platform-btn platform-btn-sm"
              :disabled="settingsBusy"
              @click="settingsOpen = false"
              >取消</button
            >
            <button
              class="platform-btn platform-btn-sm platform-btn-pri"
              :disabled="settingsBusy"
              @click="saveSettings"
            >
              {{ settingsBusy ? '保存中…' : '保存 kb.yaml' }}
            </button>
          </div>
        </div>
      </div>
    </Teleport>
    <Teleport to="body">
      <div v-if="importOpen" class="platform-modal-mask" @click.self="closeImport">
        <div
          class="platform-modal"
          role="dialog"
          aria-label="上传导入"
          style="width: min(880px, calc(100vw - 48px))"
        >
          <h3>上传导入 · {{ name }}</h3>

          <template v-if="importStep === 'pick'">
            <p>
              选择 Markdown 或 PDF 文件，服务端会清洗并按章节切分为候选文档；
              下一步可逐块确认后再批量入库。
            </p>
            <p v-if="importError" role="alert" style="color: var(--p-red)">
              ✕ {{ importError }}
            </p>
            <div style="display: flex; gap: 10px; align-items: center">
              <input
                type="file"
                accept=".md,.markdown,.pdf"
                aria-label="选择导入文件"
                :disabled="importLoading"
                @change="onImportFileChange"
              />
              <span v-if="importLoading" class="platform-mut" style="font-size: 12px"
                >正在解析文件…</span
              >
            </div>
          </template>

          <template v-else-if="importPreviewData">
            <div class="kb-import-summary">
              <span class="platform-mono">{{ importPreviewData.filename }}</span>
              <span
                ><b>{{ importPreviewData.stats.chunk_count }}</b> 个候选块</span
              >
              <span class="platform-mut"
                >字数 {{ importPreviewData.stats.min_chars }}–{{
                  importPreviewData.stats.max_chars
                }}（均值 {{ importPreviewData.stats.avg_chars }}）</span
              >
              <span
                v-if="importPreviewData.stats.broken_tables"
                style="color: var(--p-amber)"
                >表格受损 {{ importPreviewData.stats.broken_tables }}</span
              >
              <span v-if="importPreviewData.stats.dropped_images" class="platform-mut"
                >丢弃图片 {{ importPreviewData.stats.dropped_images }}</span
              >
              <span v-if="importPreviewData.stats.cleaned_lines" class="platform-mut"
                >清洗行 {{ importPreviewData.stats.cleaned_lines }}</span
              >
            </div>
            <details
              v-if="importPreviewData.warnings.length"
              class="kb-import-warnings"
            >
              <summary> 解析警告（{{ importPreviewData.warnings.length }}） </summary>
              <ul>
                <li v-for="(warning, index) in importPreviewData.warnings" :key="index">
                  {{ warning }}
                </li>
              </ul>
            </details>

            <div class="kb-import-rows">
              <div
                v-for="row in importRows"
                :key="row.chunk.index"
                class="platform-card kb-import-row"
                :class="{ 'kb-import-row-off': !row.selected }"
              >
                <input
                  v-model="row.selected"
                  type="checkbox"
                  :aria-label="`选择 ${row.chunk.suggested_doc_id}`"
                />
                <div class="kb-import-main">
                  <div class="kb-import-head">
                    <span class="platform-mono platform-mut">{{
                      row.chunk.suggested_doc_id
                    }}</span>
                    <span
                      v-if="row.chunk.duplicate_of"
                      class="platform-badge platform-b-amber"
                      >重复 → {{ row.chunk.duplicate_of }}</span
                    >
                    <span
                      v-if="row.chunk.has_table"
                      class="platform-badge platform-b-violet"
                      >含表格</span
                    >
                    <span class="platform-mut"
                      >{{ row.chunk.char_count }} 字 ·
                      {{ row.chunk.section_path }}</span
                    >
                  </div>
                  <div class="kb-import-fields">
                    <input
                      v-model="row.title"
                      type="text"
                      style="flex: 2"
                      aria-label="标题"
                    />
                    <input
                      v-model="row.category"
                      type="text"
                      style="flex: 1"
                      aria-label="分类"
                      :placeholder="row.chunk.suggested_category"
                    />
                  </div>
                  <details class="kb-import-content">
                    <summary class="platform-mut">预览内容</summary>
                    <pre>{{ row.chunk.content }}</pre>
                  </details>
                </div>
              </div>
            </div>
          </template>

          <div class="platform-modal-actions">
            <button
              v-if="importStep === 'preview'"
              class="platform-btn platform-btn-ghost platform-btn-sm"
              :disabled="importSubmitting"
              @click="importStep = 'pick'"
              >重新选择</button
            >
            <button
              class="platform-btn platform-btn-sm"
              :disabled="importSubmitting"
              @click="closeImport"
              >取消</button
            >
            <button
              v-if="importStep === 'preview'"
              class="platform-btn platform-btn-sm platform-btn-pri"
              :disabled="importSubmitting || !importSelectedCount"
              @click="confirmImport"
            >
              {{ importSubmitting ? '导入中…' : `确认导入（${importSelectedCount}）` }}
            </button>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
.kb-crumb {
  flex: none;
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 24px;
  border-bottom: 1px solid var(--p-border);
  background: rgba(6, 9, 18, 0.6);
  font-size: 13px;
}
.kb-crumb-link {
  color: var(--p-cyan);
  text-decoration: none;
}
.kb-ws-body {
  flex: 1;
  min-height: 0;
  display: flex;
}
.kb-ws-docs {
  flex: 1.4;
  min-width: 0;
  border-right: 1px solid var(--p-border);
  display: flex;
  flex-direction: column;
  overflow-y: auto;
}
.kb-ws-toolbar {
  flex: none;
  display: flex;
  gap: 8px;
  padding: 10px 14px;
  border-bottom: 1px solid var(--p-border);
}
.kb-doc-table {
  width: 100%;
  border-collapse: collapse;
}
.kb-doc-table th,
.kb-doc-table td {
  text-align: left;
  padding: 8px 12px;
  border-bottom: 1px solid var(--p-border);
  font-size: 13px;
}
.kb-doc-table th {
  color: var(--p-mut);
  font-weight: 500;
  font-size: 12px;
}
.kb-doc-table tbody tr {
  cursor: pointer;
}
.kb-doc-table tbody tr:hover td {
  background: var(--p-panel);
}
.kb-doc-table tr.selected td {
  background: var(--p-panel2);
}
.kb-ws-editor {
  flex: 1;
  min-width: 0;
  padding: 18px 22px;
  overflow-y: auto;
  background: rgba(6, 9, 18, 0.35);
}
.kb-import-summary {
  display: flex;
  gap: 14px;
  align-items: center;
  flex-wrap: wrap;
  font-size: 12.5px;
  padding: 4px 0 8px;
}
.kb-import-warnings {
  font-size: 12.5px;
  color: var(--p-amber);
  margin-bottom: 4px;
}
.kb-import-warnings ul {
  margin: 6px 0 0;
  padding-left: 18px;
  color: var(--p-mut);
}
.kb-import-rows {
  max-height: 52vh;
  overflow-y: auto;
  margin-top: 6px;
}
.kb-import-row {
  display: flex;
  gap: 12px;
  align-items: flex-start;
  padding: 10px 12px;
  margin-top: 8px;
}
.kb-import-row input[type='checkbox'] {
  margin-top: 3px;
}
.kb-import-row-off {
  opacity: 0.55;
}
.kb-import-main {
  flex: 1;
  min-width: 0;
}
.kb-import-head {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
  font-size: 12px;
}
.kb-import-fields {
  display: flex;
  gap: 8px;
  margin-top: 8px;
}
.kb-import-content {
  margin-top: 8px;
  font-size: 12px;
}
.kb-import-content pre {
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 200px;
  overflow-y: auto;
  margin: 6px 0 0;
  padding: 8px 10px;
  border-radius: 6px;
  background: rgba(6, 9, 18, 0.5);
  font-size: 12px;
}
</style>
