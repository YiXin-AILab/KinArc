import { createHttpTransport } from '../api/httpTransport'
import { loadRuntimeConfig } from '../config'

/** 知识库列表摘要，对齐 server/platform_kb.py PlatformKbSummary。 */
export interface KbSummary {
  name: string
  scope: string
  description: string
  doc_count: number | null
  strategy: string | null
  top_k: number | null
  has_rerank: boolean
  has_gate: boolean
  referenced_by: string[]
  valid: boolean
  error: string | null
}

/** kb.yaml 原文（${VAR} 占位符保持原样），对齐 PlatformKbManifestResponse。 */
export interface KbManifest {
  name: string
  scope: string
  config: Record<string, unknown>
}

/** 一条知识库文档，对齐 PlatformKbDocument。 */
export interface KbDocument {
  doc_id: string
  title: string
  category: string
  content: string
  updated_at: string
}

/** 导入预览中的单个切分块，对齐 PlatformKbImportChunk。 */
export interface KbImportPreviewChunk {
  index: number
  suggested_doc_id: string
  suggested_title: string
  suggested_category: string
  content: string
  section_path: string
  has_table: boolean
  char_count: number
  duplicate_of: string | null
}

/** 导入预览的切分统计，对齐 PlatformKbImportStats。 */
export interface KbImportPreviewStats {
  chunk_count: number
  min_chars: number
  max_chars: number
  avg_chars: number
  broken_tables: number
  dropped_images: number
  cleaned_lines: number
}

/** 上传文件解析预览，对齐 PlatformKbImportPreviewResponse。 */
export interface KbImportPreview {
  knowledge_base: string
  filename: string
  media_type: string
  chunks: KbImportPreviewChunk[]
  warnings: string[]
  stats: KbImportPreviewStats
}

/** 检索覆盖配置；缺省字段跟随库默认，只影响本次调用。 */
export interface KbRetrievalOverrides {
  strategy?: 'bm25' | 'vector' | 'hybrid'
  top_k?: number
  rerank?: Record<string, unknown>
  gate?: Record<string, unknown>
}

export interface KbSearchRequest extends KbRetrievalOverrides {
  query: string
  doc_scope?: string[]
}

/** 一个检索片段及其裁判结果，对齐 PlatformKbSearchChunk。 */
export interface KbSearchChunk {
  doc_id: string
  title: string
  content: string
  score: number
  gate_relevance: string | null
  gate_reason: string | null
  gate_error: boolean
  filtered: boolean
}

export interface KbSearchResponse {
  knowledge_base: string
  query: string
  chunks: KbSearchChunk[]
  no_relevant_docs: boolean
}

/** 一道批测题，对齐 PlatformKbBatchQuestion。 */
export interface KbBatchQuestion {
  question: string
  expected_doc_ids?: string[]
  reference_answer?: string
}

export interface KbBatchSearchRequest extends KbRetrievalOverrides {
  questions?: KbBatchQuestion[]
  question_set?: string
  concurrency?: number
}

export interface KbMissingDoc {
  doc_id: string
  rank: number | null
  gate_filtered: boolean
}

export interface KbDocMetrics {
  expected_doc_ids: string[]
  top1_hit: boolean
  top3_hit: boolean
  set_recall: boolean
  gate_false_kills: string[]
  missing: KbMissingDoc[]
}

export interface KbAnswerDiff {
  matched: string[]
  missing: string[]
  extra: string[]
}

export interface KbAnswerMetrics {
  coverage: number | null
  rationale: string
  diff: KbAnswerDiff
  diff_note: string
  error: string | null
}

export interface KbBatchItem {
  question: string
  chunks: KbSearchChunk[]
  no_relevant_docs: boolean
  doc_metrics: KbDocMetrics | null
  answer_metrics: KbAnswerMetrics | null
  error: string | null
}

export interface KbBatchSummary {
  doc_questions: {
    count: number
    top1_rate: number
    top3_rate: number
    set_recall_rate: number
    gate_false_kill_count: number
  }
  answer_questions: {
    count: number
    judged_count: number
    average_coverage: number | null
  }
}

export interface KbBatchSearchResponse {
  knowledge_base: string
  results: KbBatchItem[]
  summary: KbBatchSummary
}

export interface KbGenerateRequest {
  sources: string[]
  guidance: string
  count: number
}

export interface KbGenKbResult {
  source: string
  status: string
  question_set: string | null
  generated: number
  kept: number
  type_counts: Record<string, number>
  shortfall: Record<string, number>
  error: string | null
}

/** 生成 Job 快照，对齐 PlatformKbGenJobResponse。 */
export interface KbGenJob {
  job_id: string
  status: 'pending' | 'running' | 'succeeded' | 'failed'
  created_at: number
  finished_at: number | null
  sources: string[]
  count: number
  results: KbGenKbResult[]
  error: string | null
}

export type KbCaseType = 'single_doc' | 'multi_doc' | 'boundary' | 'out_of_scope'
export type KbQuestionStatus = 'pending' | 'accepted' | 'discarded'

/** 问题集条目，对齐 QuestionSetItem。 */
export interface KbQuestionItem {
  question: string
  expected_doc_ids: string[]
  reference_answer: string | null
  case_type: KbCaseType
  status: KbQuestionStatus
  edited: boolean
  forbidden: string[]
}

export interface KbQuestionSet {
  name: string
  created_at: string
  source: 'generated' | 'manual'
  guidance: string | null
  items: KbQuestionItem[]
}

export interface KbQuestionSetSummary {
  name: string
  created_at: string | null
  source: string | null
  total: number
  pending: number
  accepted: number
  discarded: number
  valid: boolean
  error: string | null
}

export interface KbQuestionItemPatch {
  status?: KbQuestionStatus
  question?: string
  expected_doc_ids?: string[]
  reference_answer?: string
}

export interface KbToEvalDatasetResult {
  task_id: string
  dataset: string
  appended: number
  skipped: number
  total_cases: number
}

export interface KbClient {
  listBases(signal?: AbortSignal): Promise<KbSummary[]>
  createBase(
    payload: { scope: string; name: string; description: string },
    signal?: AbortSignal,
  ): Promise<KbSummary>
  getManifest(scope: string, name: string, signal?: AbortSignal): Promise<KbManifest>
  updateManifest(
    scope: string,
    name: string,
    config: Record<string, unknown>,
    signal?: AbortSignal,
  ): Promise<KbManifest>
  listDocs(scope: string, name: string, signal?: AbortSignal): Promise<KbDocument[]>
  createDoc(
    scope: string,
    name: string,
    doc: KbDocument,
    signal?: AbortSignal,
  ): Promise<KbDocument>
  updateDoc(
    scope: string,
    name: string,
    doc: KbDocument,
    signal?: AbortSignal,
  ): Promise<KbDocument>
  deleteDoc(scope: string, name: string, docId: string): Promise<void>
  importPreview(
    scope: string,
    name: string,
    file: File,
    signal?: AbortSignal,
  ): Promise<KbImportPreview>
  importDocs(
    scope: string,
    name: string,
    docs: KbDocument[],
    signal?: AbortSignal,
  ): Promise<void>
  search(
    scope: string,
    name: string,
    request: KbSearchRequest,
    signal?: AbortSignal,
  ): Promise<KbSearchResponse>
  batchSearch(
    scope: string,
    name: string,
    request: KbBatchSearchRequest,
    signal?: AbortSignal,
  ): Promise<KbBatchSearchResponse>
  generate(request: KbGenerateRequest, signal?: AbortSignal): Promise<KbGenJob>
  getGenJob(jobId: string, signal?: AbortSignal): Promise<KbGenJob>
  listQuestionSets(
    scope: string,
    name: string,
    signal?: AbortSignal,
  ): Promise<KbQuestionSetSummary[]>
  getQuestionSet(
    scope: string,
    name: string,
    setName: string,
    signal?: AbortSignal,
  ): Promise<KbQuestionSet>
  patchQuestionItem(
    scope: string,
    name: string,
    setName: string,
    index: number,
    patch: KbQuestionItemPatch,
    signal?: AbortSignal,
  ): Promise<KbQuestionItem>
  acceptAll(scope: string, name: string, setName: string): Promise<void>
  toEvalDataset(
    scope: string,
    name: string,
    setName: string,
    payload: { task_id: string; dataset: string },
  ): Promise<KbToEvalDatasetResult>
}

export interface KbClientConfig {
  apiBaseUrl: string
  tokenProvider?: () => string | null
}

export function createKbClient(
  config: KbClientConfig,
  fetchImpl: typeof fetch = fetch,
): KbClient {
  const base = `${config.apiBaseUrl}/v1/platform`
  const transport = createHttpTransport(config, fetchImpl)

  const kbPath = (scope: string, name: string, suffix = ''): string =>
    `/knowledge-bases/${encodeURIComponent(scope)}/${encodeURIComponent(name)}${suffix}`

  const request = <T>(
    path: string,
    options: {
      method?: string
      body?: unknown
      formData?: FormData
      signal?: AbortSignal
    } = {},
  ): Promise<T> => transport.requestJson<T>(`${base}${path}`, options)

  return {
    async listBases(signal) {
      const body = await request<{ knowledge_bases: KbSummary[] }>('/knowledge-bases', {
        signal,
      })
      return body.knowledge_bases
    },
    async createBase(payload, signal) {
      const body = await request<{ knowledge_base: KbSummary }>('/knowledge-bases', {
        method: 'POST',
        body: payload,
        signal,
      })
      return body.knowledge_base
    },
    getManifest(scope, name, signal) {
      return request<KbManifest>(kbPath(scope, name), { signal })
    },
    updateManifest(scope, name, configBody, signal) {
      return request<KbManifest>(kbPath(scope, name), {
        method: 'PUT',
        body: { config: configBody },
        signal,
      })
    },
    async listDocs(scope, name, signal) {
      const body = await request<{ docs: KbDocument[] }>(kbPath(scope, name, '/docs'), {
        signal,
      })
      return body.docs
    },
    async createDoc(scope, name, doc, signal) {
      const body = await request<{ doc: KbDocument }>(kbPath(scope, name, '/docs'), {
        method: 'POST',
        body: doc,
        signal,
      })
      return body.doc
    },
    async updateDoc(scope, name, doc, signal) {
      const body = await request<{ doc: KbDocument }>(
        kbPath(scope, name, `/docs/${encodeURIComponent(doc.doc_id)}`),
        { method: 'PUT', body: doc, signal },
      )
      return body.doc
    },
    async deleteDoc(scope, name, docId) {
      await request<{ doc: KbDocument }>(
        kbPath(scope, name, `/docs/${encodeURIComponent(docId)}`),
        { method: 'DELETE' },
      )
    },
    importPreview(scope, name, file, signal) {
      const formData = new FormData()
      formData.append('file', file)
      return request<KbImportPreview>(kbPath(scope, name, '/docs/import-preview'), {
        method: 'POST',
        formData,
        signal,
      })
    },
    async importDocs(scope, name, docs, signal) {
      await request<unknown>(kbPath(scope, name, '/docs/import'), {
        method: 'POST',
        body: { docs },
        signal,
      })
    },
    search(scope, name, searchRequest, signal) {
      return request<KbSearchResponse>(kbPath(scope, name, '/search'), {
        method: 'POST',
        body: searchRequest,
        signal,
      })
    },
    batchSearch(scope, name, batchRequest, signal) {
      return request<KbBatchSearchResponse>(kbPath(scope, name, '/batch-search'), {
        method: 'POST',
        body: batchRequest,
        signal,
      })
    },
    async generate(generateRequest, signal) {
      const body = await request<{ job_id: string; status: KbGenJob['status'] }>(
        '/knowledge-bases/generate',
        { method: 'POST', body: generateRequest, signal },
      )
      return {
        job_id: body.job_id,
        status: body.status,
        created_at: 0,
        finished_at: null,
        sources: [],
        count: 0,
        results: [],
        error: null,
      }
    },
    getGenJob(jobId, signal) {
      return request<KbGenJob>(`/kb-gen-jobs/${encodeURIComponent(jobId)}`, { signal })
    },
    async listQuestionSets(scope, name, signal) {
      const body = await request<{ question_sets: KbQuestionSetSummary[] }>(
        kbPath(scope, name, '/question_sets'),
        { signal },
      )
      return body.question_sets
    },
    async getQuestionSet(scope, name, setName, signal) {
      const body = await request<{ question_set: KbQuestionSet }>(
        kbPath(scope, name, `/question_sets/${encodeURIComponent(setName)}`),
        { signal },
      )
      return body.question_set
    },
    async patchQuestionItem(scope, name, setName, index, patch, signal) {
      const body = await request<{ index: number; item: KbQuestionItem }>(
        kbPath(
          scope,
          name,
          `/question_sets/${encodeURIComponent(setName)}/items/${index}`,
        ),
        { method: 'PATCH', body: patch, signal },
      )
      return body.item
    },
    async acceptAll(scope, name, setName) {
      await request<KbQuestionSetSummary>(
        kbPath(scope, name, `/question_sets/${encodeURIComponent(setName)}/accept-all`),
        { method: 'POST', body: {} },
      )
    },
    toEvalDataset(scope, name, setName, payload) {
      return request<KbToEvalDatasetResult>(
        kbPath(
          scope,
          name,
          `/question_sets/${encodeURIComponent(setName)}/to-eval-dataset`,
        ),
        { method: 'POST', body: payload },
      )
    },
  }
}

/** 页面默认使用的知识库客户端；测试中 mock 本模块即可替换。 */
export function defaultKbClient(): KbClient {
  const config = loadRuntimeConfig(import.meta.env)
  return createKbClient({
    apiBaseUrl: config.apiBaseUrl,
    tokenProvider: () => sessionStorage.getItem('agentsystem.auth-token'),
  })
}

/** 解析生成请求的 source 串：`name`（全局）或 `<agent_id>/<name>`。 */
export function parseKbSource(source: string): { scope: string; name: string } {
  const slash = source.indexOf('/')
  if (slash < 0) return { scope: 'global', name: source }
  return { scope: source.slice(0, slash), name: source.slice(slash + 1) }
}

/** 批测上传文件模板（问题列必填，答案列可选）与解析。 */
export const BATCH_TEMPLATE_HEADER = 'question,expected_doc_ids,reference_answer'

export function parseBatchCsv(text: string): KbBatchQuestion[] {
  const rows = parseCsvRows(text)
  const questions: KbBatchQuestion[] = []
  for (const [rowIndex, cells] of rows.entries()) {
    if (rowIndex === 0 && cells[0]?.trim().toLowerCase() === 'question') continue
    const question = cells[0]?.trim() ?? ''
    if (!question) continue
    const item: KbBatchQuestion = { question }
    const docIds = (cells[1] ?? '')
      .split('|')
      .map((part) => part.trim())
      .filter(Boolean)
    if (docIds.length) item.expected_doc_ids = docIds
    const answer = cells[2]?.trim()
    if (answer) item.reference_answer = answer
    questions.push(item)
  }
  return questions
}

function parseCsvRows(text: string): string[][] {
  const rows: string[][] = []
  let cells: string[] = []
  let cell = ''
  let quoted = false
  const pushCell = (): void => {
    cells.push(cell)
    cell = ''
  }
  const pushRow = (): void => {
    pushCell()
    rows.push(cells)
    cells = []
  }
  for (let index = 0; index < text.length; index += 1) {
    const char = text[index]
    if (quoted) {
      if (char === '"' && text[index + 1] === '"') {
        cell += '"'
        index += 1
      } else if (char === '"') {
        quoted = false
      } else {
        cell += char
      }
    } else if (char === '"') {
      quoted = true
    } else if (char === ',') {
      pushCell()
    } else if (char === '\n' || char === '\r') {
      if (char === '\r' && text[index + 1] === '\n') index += 1
      pushRow()
    } else {
      cell += char
    }
  }
  if (cell !== '' || cells.length) pushRow()
  return rows.filter((row) => row.some((value) => value.trim() !== ''))
}
