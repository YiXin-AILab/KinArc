import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory } from 'vue-router'
import type { Component } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { createAppRouter } from '../../router'
import KbGeneratePage from '../KbGeneratePage.vue'
import KbListPage from '../KbListPage.vue'
import KbTestPage from '../KbTestPage.vue'
import KbWorkspacePage from '../KbWorkspacePage.vue'

const { kbClient, platformClient } = vi.hoisted(() => ({
  kbClient: {
    listBases: vi.fn(),
    createBase: vi.fn(),
    getManifest: vi.fn(),
    updateManifest: vi.fn(),
    listDocs: vi.fn(),
    createDoc: vi.fn(),
    updateDoc: vi.fn(),
    deleteDoc: vi.fn(),
    search: vi.fn(),
    batchSearch: vi.fn(),
    generate: vi.fn(),
    getGenJob: vi.fn(),
    listQuestionSets: vi.fn(),
    getQuestionSet: vi.fn(),
    patchQuestionItem: vi.fn(),
    acceptAll: vi.fn(),
    toEvalDataset: vi.fn(),
  },
  platformClient: { listAgents: vi.fn() },
}))

vi.mock('../kbClient', () => ({
  defaultKbClient: () => kbClient,
  parseKbSource: (source: string) => {
    const slash = source.indexOf('/')
    return slash < 0
      ? { scope: 'global', name: source }
      : { scope: source.slice(0, slash), name: source.slice(slash + 1) }
  },
  parseBatchCsv: vi.fn(),
  BATCH_TEMPLATE_HEADER: 'question,expected_doc_ids,reference_answer',
}))
vi.mock('../client', () => ({ defaultPlatformClient: () => platformClient }))

const KB_PRICING = {
  name: 'pricing',
  scope: 'global',
  description: '价格/工时费咨询库',
  doc_count: 22,
  strategy: 'bm25',
  top_k: 5,
  has_rerank: false,
  has_gate: true,
  referenced_by: ['auto-after-sales'],
  valid: true,
  error: null,
}

const KB_VIP = {
  ...KB_PRICING,
  name: 'vip_policy',
  scope: 'auto-after-sales',
  description: '会员权益',
  doc_count: 6,
  strategy: 'vector',
  has_gate: false,
  referenced_by: [],
}

const DOC = {
  doc_id: 'pricing-007',
  title: 'S4 A 保价格与时长',
  category: '保养',
  content: 'S4 A 保参考价格 1400-1700 元。',
  updated_at: '2026-09-12',
}

const QUESTION_SET = {
  name: 'gen-20261001-011718',
  created_at: '2026-10-01T01:18:10Z',
  source: 'generated',
  guidance: null,
  items: [
    {
      question: 'S4 A 保多少钱？',
      expected_doc_ids: ['pricing-007'],
      reference_answer: 'A 保 1400-1700 元',
      case_type: 'single_doc',
      status: 'pending',
      edited: false,
      forbidden: [],
    },
    {
      question: 'T3 保养多少钱？',
      expected_doc_ids: [],
      reference_answer: null,
      case_type: 'out_of_scope',
      status: 'accepted',
      edited: false,
      forbidden: [],
    },
  ],
}

async function mountPage(component: Component, path: string) {
  const router = createAppRouter(createMemoryHistory())
  await router.push(path)
  await router.isReady()
  const wrapper = mount(component, {
    global: { plugins: [router] },
  })
  await flushPromises()
  return wrapper
}

describe('KbListPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    kbClient.listBases.mockResolvedValue([KB_PRICING, KB_VIP])
    platformClient.listAgents.mockResolvedValue([])
  })

  it('renders kb cards with stats, scope badge and references', async () => {
    const wrapper = await mountPage(KbListPage, '/platform/knowledge')

    expect(wrapper.text()).toContain('知识库资源')
    expect(wrapper.text()).toContain('✨ 生成评测集')
    expect(wrapper.text()).toContain('🔍 检索测试')
    expect(wrapper.text()).toContain('＋ 新建知识库')
    expect(wrapper.text()).toContain('pricing')
    expect(wrapper.text()).toContain('22 篇文档')
    expect(wrapper.text()).toContain('默认 bm25')
    expect(wrapper.text()).toContain('被 1 个 Agent 引用：auto-after-sales')
    expect(wrapper.text()).toContain('auto-after-sales 应用')
    wrapper.unmount()
  })

  it('creates a kb and navigates to its workspace', async () => {
    kbClient.createBase.mockResolvedValue(KB_VIP)
    const wrapper = await mountPage(KbListPage, '/platform/knowledge')

    const createButton = wrapper
      .findAll('button')
      .find((button) => button.text().includes('新建知识库'))
    await createButton!.trigger('click')
    const modal = document.querySelector('.platform-modal')!
    const nameInput = modal.querySelector('input') as HTMLInputElement
    nameInput.value = 'vip_policy'
    nameInput.dispatchEvent(new Event('input'))
    const confirm = [...modal.querySelectorAll('button')].find((button) =>
      button.textContent?.includes('创建'),
    ) as HTMLButtonElement
    confirm.click()
    await flushPromises()

    expect(kbClient.createBase).toHaveBeenCalledWith({
      scope: 'global',
      name: 'vip_policy',
      description: '',
    })
    wrapper.unmount()
  })
})

describe('KbWorkspacePage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    kbClient.listBases.mockResolvedValue([KB_PRICING])
    kbClient.listDocs.mockResolvedValue([
      DOC,
      { ...DOC, doc_id: 'pricing-009', title: '维修工时费标准' },
    ])
  })

  it('renders the doc table with update time and selects a row into the editor', async () => {
    const wrapper = await mountPage(
      KbWorkspacePage,
      '/platform/knowledge/global/pricing',
    )

    expect(wrapper.text()).toContain('pricing-007')
    expect(wrapper.text()).toContain('更新时间')
    expect(wrapper.text()).toContain('默认策略 bm25 · 裁判已启用')
    const rows = wrapper.findAll('.kb-doc-table tbody tr')
    await rows[1]!.trigger('click')
    const titleInput = wrapper.get('input[aria-label="标题"]')
    expect((titleInput.element as HTMLInputElement).value).toBe('维修工时费标准')
    wrapper.unmount()
  })

  it('saves the edited doc via updateDoc', async () => {
    kbClient.updateDoc.mockResolvedValue(DOC)
    const wrapper = await mountPage(
      KbWorkspacePage,
      '/platform/knowledge/global/pricing',
    )

    const titleInput = wrapper.get('input[aria-label="标题"]')
    await titleInput.setValue('S4 A 保价格（更新）')
    const saveButton = wrapper
      .findAll('button')
      .find((button) => button.text() === '保存')
    await saveButton!.trigger('click')
    await flushPromises()

    expect(kbClient.updateDoc).toHaveBeenCalledWith(
      'global',
      'pricing',
      expect.objectContaining({
        doc_id: 'pricing-007',
        title: 'S4 A 保价格（更新）',
      }),
    )
    wrapper.unmount()
  })
})

describe('KbTestPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    kbClient.listBases.mockResolvedValue([KB_PRICING])
    kbClient.listDocs.mockResolvedValue([DOC])
    kbClient.listQuestionSets.mockResolvedValue([
      {
        name: 'gen-20261001-011718',
        created_at: null,
        source: 'generated',
        total: 2,
        pending: 1,
        accepted: 1,
        discarded: 0,
        valid: true,
        error: null,
      },
    ])
    kbClient.getQuestionSet.mockResolvedValue(QUESTION_SET)
  })

  it('runs a single search and renders chunks with gate verdicts', async () => {
    kbClient.search.mockResolvedValue({
      knowledge_base: 'pricing',
      query: 'S4 A 保多少钱',
      no_relevant_docs: false,
      chunks: [
        {
          doc_id: 'pricing-007',
          title: DOC.title,
          content: DOC.content,
          score: 0.82,
          gate_relevance: 'relevant',
          gate_reason: null,
          gate_error: false,
          filtered: false,
        },
        {
          doc_id: 'pricing-009',
          title: '维修工时费标准',
          content: '工时费 380 元/小时',
          score: 0.4,
          gate_relevance: 'irrelevant',
          gate_reason: '与问题无关',
          gate_error: false,
          filtered: true,
        },
      ],
    })
    const wrapper = await mountPage(
      KbTestPage,
      '/platform/knowledge/global/pricing/test',
    )

    await wrapper.get('textarea[aria-label="测试问题"]').setValue('S4 A 保多少钱')
    await wrapper.get('[data-testid="run-single"]').trigger('click')
    await flushPromises()

    expect(kbClient.search).toHaveBeenCalledWith(
      'global',
      'pricing',
      expect.objectContaining({ query: 'S4 A 保多少钱' }),
    )
    expect(wrapper.text()).toContain('relevant ✓')
    expect(wrapper.text()).toContain('irrelevant ✗ 被裁判剔除')
    expect(wrapper.text()).toContain('裁判理由：与问题无关')
    const filtered = wrapper.findAll('.kb-chunk-filtered')
    expect(filtered).toHaveLength(1)
    wrapper.unmount()
  })

  it('narrows doc_scope when only some docs are checked', async () => {
    kbClient.listDocs.mockResolvedValue([
      DOC,
      { ...DOC, doc_id: 'pricing-009', title: '维修工时费标准' },
    ])
    kbClient.search.mockResolvedValue({
      knowledge_base: 'pricing',
      query: 'q',
      chunks: [],
      no_relevant_docs: false,
    })
    const wrapper = await mountPage(
      KbTestPage,
      '/platform/knowledge/global/pricing/test',
    )

    const checkboxes = wrapper.findAll('.kb-scope-list input[type="checkbox"]')
    await checkboxes[1]!.setValue(false)
    await wrapper.get('textarea[aria-label="测试问题"]').setValue('q')
    await wrapper.get('[data-testid="run-single"]').trigger('click')
    await flushPromises()

    expect(kbClient.search).toHaveBeenCalledWith(
      'global',
      'pricing',
      expect.objectContaining({ doc_scope: ['pricing-007'] }),
    )
    wrapper.unmount()
  })

  it('runs a batch test from a question set and renders the metric bar and diff', async () => {
    kbClient.batchSearch.mockResolvedValue({
      knowledge_base: 'pricing',
      summary: {
        doc_questions: {
          count: 2,
          top1_rate: 0.75,
          top3_rate: 0.875,
          set_recall_rate: 0.5,
          gate_false_kill_count: 1,
        },
        answer_questions: { count: 1, judged_count: 1, average_coverage: 0.82 },
      },
      results: [
        {
          question: 'S4 A 保多少钱',
          no_relevant_docs: false,
          error: null,
          chunks: [
            {
              doc_id: 'pricing-007',
              title: DOC.title,
              content: DOC.content,
              score: 0.82,
              gate_relevance: 'relevant',
              gate_reason: null,
              gate_error: false,
              filtered: false,
            },
          ],
          doc_metrics: {
            expected_doc_ids: ['pricing-007'],
            top1_hit: true,
            top3_hit: true,
            set_recall: true,
            gate_false_kills: [],
            missing: [],
          },
          answer_metrics: null,
        },
        {
          question: '服务会员有什么权益',
          no_relevant_docs: false,
          error: null,
          chunks: [
            {
              doc_id: 'pricing-018',
              title: '会员权益',
              content: '服务会员享工时费 9 折',
              score: 0.71,
              gate_relevance: null,
              gate_reason: null,
              gate_error: false,
              filtered: false,
            },
          ],
          doc_metrics: null,
          answer_metrics: {
            coverage: 0.85,
            rationale: '核心权益一致',
            diff: {
              matched: ['工时费', '9 折'],
              missing: ['生日折扣'],
              extra: ['机场接送'],
            },
            diff_note: 'diff 为词级重叠 best-effort 结果',
            error: null,
          },
        },
      ],
    })
    const wrapper = await mountPage(
      KbTestPage,
      '/platform/knowledge/global/pricing/test?tab=batch',
    )

    expect(wrapper.find('[data-testid="tab-batch"]').classes()).toContain('on')
    await wrapper.get('[data-testid="run-batch"]').trigger('click')
    await flushPromises()

    expect(kbClient.batchSearch).toHaveBeenCalledWith(
      'global',
      'pricing',
      expect.objectContaining({ question_set: 'gen-20261001-011718' }),
    )
    const bar = wrapper.get('[data-testid="batch-metric-bar"]')
    expect(bar.text()).toContain('75%')
    expect(bar.text()).toContain('87.5%')
    expect(bar.text()).toContain('裁判误杀')
    expect(bar.text()).toContain('0.82')
    expect(wrapper.text()).toContain('期望 pricing-007')
    expect(wrapper.text()).toContain('内容覆盖分 0.85')
    expect(wrapper.findAll('.kb-diff-same').length).toBeGreaterThanOrEqual(2)
    expect(wrapper.find('.kb-diff-miss').text()).toContain('生日折扣')
    expect(wrapper.find('.kb-diff-extra').text()).toContain('机场接送')
    expect(wrapper.text()).toContain('judge 理由：核心权益一致')
    wrapper.unmount()
  })
})

describe('KbGeneratePage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    kbClient.listBases.mockResolvedValue([KB_PRICING])
    kbClient.getQuestionSet.mockResolvedValue(QUESTION_SET)
  })

  it('loads a review queue from query params and toggles item status', async () => {
    kbClient.patchQuestionItem.mockResolvedValue({
      ...QUESTION_SET.items[0],
      status: 'accepted',
    })
    const wrapper = await mountPage(
      KbGeneratePage,
      '/platform/knowledge/generate?scope=global&name=pricing&set=gen-20261001-011718',
    )

    expect(wrapper.text()).toContain('S4 A 保多少钱？')
    expect(wrapper.get('[data-testid="review-summary"]').text()).toContain('候选 2')
    expect(wrapper.get('[data-testid="review-summary"]').text()).toContain('待审 1')

    await wrapper.get('[data-testid="accept-item"]').trigger('click')
    await flushPromises()

    expect(kbClient.patchQuestionItem).toHaveBeenCalledWith(
      'global',
      'pricing',
      'gen-20261001-011718',
      0,
      { status: 'accepted' },
    )
    expect(wrapper.findAll('.kb-draft')[0]!.classes()).toContain('kb-draft-accepted')
    wrapper.unmount()
  })

  it('starts a generate job, polls it and loads the resulting queue', async () => {
    kbClient.generate.mockResolvedValue({
      job_id: 'gen-abc',
      status: 'pending',
      created_at: 0,
      finished_at: null,
      sources: [],
      count: 0,
      results: [],
      error: null,
    })
    kbClient.getGenJob.mockResolvedValue({
      job_id: 'gen-abc',
      status: 'succeeded',
      created_at: 1,
      finished_at: 2,
      sources: ['pricing'],
      count: 10,
      results: [
        {
          source: 'pricing',
          status: 'succeeded',
          question_set: 'gen-20261001-011718',
          generated: 12,
          kept: 10,
          type_counts: { single_doc: 8, out_of_scope: 2 },
          shortfall: {},
          error: null,
        },
      ],
      error: null,
    })
    const wrapper = await mountPage(KbGeneratePage, '/platform/knowledge/generate')

    await wrapper.get('[data-testid="run-generate"]').trigger('click')
    await flushPromises()

    expect(kbClient.generate).toHaveBeenCalledWith({
      sources: ['pricing'],
      guidance: '',
      count: 10,
    })
    expect(kbClient.getGenJob).toHaveBeenCalledWith('gen-abc', expect.anything())
    expect(wrapper.text()).toContain('生成 12 · 保留 10')
    expect(wrapper.text()).toContain('S4 A 保多少钱？')
    wrapper.unmount()
  })

  it('edits a pending item and saves via patch', async () => {
    kbClient.patchQuestionItem.mockResolvedValue({
      ...QUESTION_SET.items[0],
      question: 'S4 A 保多少钱（改）',
      edited: true,
    })
    const wrapper = await mountPage(
      KbGeneratePage,
      '/platform/knowledge/generate?scope=global&name=pricing&set=gen-20261001-011718',
    )

    const editButton = wrapper
      .findAll('.kb-draft')[0]!
      .findAll('button')
      .find((button) => button.text() === '编辑')
    await editButton!.trigger('click')
    await wrapper.get('textarea[aria-label="编辑问题"]').setValue('S4 A 保多少钱（改）')
    await wrapper.get('[data-testid="save-edit"]').trigger('click')
    await flushPromises()

    expect(kbClient.patchQuestionItem).toHaveBeenCalledWith(
      'global',
      'pricing',
      'gen-20261001-011718',
      0,
      expect.objectContaining({
        question: 'S4 A 保多少钱（改）',
        expected_doc_ids: ['pricing-007'],
        reference_answer: 'A 保 1400-1700 元',
      }),
    )
    wrapper.unmount()
  })
})
