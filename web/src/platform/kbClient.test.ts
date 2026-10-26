import { describe, expect, it, vi } from 'vitest'

import { RuntimeHttpError } from '../api/client'
import { createKbClient, parseBatchCsv, parseKbSource, type KbGenJob } from './kbClient'

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function mockFetch(payload: unknown, status = 200) {
  return vi
    .fn<typeof fetch>()
    .mockImplementation(() => Promise.resolve(jsonResponse(payload, status)))
}

describe('createKbClient', () => {
  it('lists bases and unwraps the knowledge_bases field', async () => {
    const fetchImpl = mockFetch({
      knowledge_bases: [{ name: 'pricing', scope: 'global' }],
    })
    const client = createKbClient({ apiBaseUrl: 'http://api' }, fetchImpl)

    const bases = await client.listBases()

    expect(fetchImpl).toHaveBeenCalledWith(
      'http://api/v1/platform/knowledge-bases',
      expect.objectContaining({ method: 'GET' }),
    )
    expect(bases).toEqual([{ name: 'pricing', scope: 'global' }])
  })

  it('encodes scope/name path segments for doc operations', async () => {
    const fetchImpl = mockFetch({ doc: { doc_id: 'pricing-007' } })
    const client = createKbClient({ apiBaseUrl: '' }, fetchImpl)

    await client.deleteDoc('global', 'pricing', 'pricing-007')

    expect(fetchImpl).toHaveBeenCalledWith(
      '/v1/platform/knowledge-bases/global/pricing/docs/pricing-007',
      expect.objectContaining({ method: 'DELETE' }),
    )
  })

  it('sends search overrides and doc_scope in the body', async () => {
    const fetchImpl = mockFetch({
      knowledge_base: 'pricing',
      query: 'q',
      chunks: [],
      no_relevant_docs: false,
    })
    const client = createKbClient({ apiBaseUrl: '' }, fetchImpl)

    await client.search('global', 'pricing', {
      query: 'q',
      strategy: 'hybrid',
      gate: { enabled: false },
      doc_scope: ['pricing-007'],
    })

    const [url, init] = fetchImpl.mock.calls[0]!
    expect(url).toBe('/v1/platform/knowledge-bases/global/pricing/search')
    expect(JSON.parse(String(init?.body))).toEqual({
      query: 'q',
      strategy: 'hybrid',
      gate: { enabled: false },
      doc_scope: ['pricing-007'],
    })
  })

  it('sends question_set batch requests to batch-search', async () => {
    const fetchImpl = mockFetch({
      knowledge_base: 'pricing',
      results: [],
      summary: {
        doc_questions: {
          count: 0,
          top1_rate: 0,
          top3_rate: 0,
          set_recall_rate: 0,
          gate_false_kill_count: 0,
        },
        answer_questions: { count: 0, judged_count: 0, average_coverage: null },
      },
    })
    const client = createKbClient({ apiBaseUrl: '' }, fetchImpl)

    await client.batchSearch('global', 'pricing', { question_set: 'gen-1' })

    const [url, init] = fetchImpl.mock.calls[0]!
    expect(url).toBe('/v1/platform/knowledge-bases/global/pricing/batch-search')
    expect(JSON.parse(String(init?.body))).toEqual({ question_set: 'gen-1' })
  })

  it('starts a generate job and maps the 202 response', async () => {
    const fetchImpl = mockFetch({ job_id: 'gen-abc', status: 'pending' }, 202)
    const client = createKbClient({ apiBaseUrl: '' }, fetchImpl)

    const job = await client.generate({ sources: ['pricing'], guidance: '', count: 10 })

    expect(fetchImpl.mock.calls[0]![0]).toBe('/v1/platform/knowledge-bases/generate')
    expect(job.job_id).toBe('gen-abc')
  })

  it('polls gen job status', async () => {
    const job: KbGenJob = {
      job_id: 'gen-abc',
      status: 'succeeded',
      created_at: 1,
      finished_at: 2,
      sources: ['pricing'],
      count: 10,
      results: [],
      error: null,
    }
    const fetchImpl = mockFetch(job)
    const client = createKbClient({ apiBaseUrl: '' }, fetchImpl)

    const snapshot = await client.getGenJob('gen-abc')

    expect(fetchImpl.mock.calls[0]![0]).toBe('/v1/platform/kb-gen-jobs/gen-abc')
    expect(snapshot.status).toBe('succeeded')
  })

  it('patches a question item and unwraps the item field', async () => {
    const fetchImpl = mockFetch({
      index: 2,
      item: { question: 'q', status: 'accepted' },
    })
    const client = createKbClient({ apiBaseUrl: '' }, fetchImpl)

    const item = await client.patchQuestionItem('global', 'pricing', 'gen-1', 2, {
      status: 'accepted',
    })

    expect(fetchImpl.mock.calls[0]![0]).toBe(
      '/v1/platform/knowledge-bases/global/pricing/question_sets/gen-1/items/2',
    )
    expect(fetchImpl.mock.calls[0]![1]?.method).toBe('PATCH')
    expect(item.status).toBe('accepted')
  })

  it('maps backend error bodies to RuntimeHttpError', async () => {
    const fetchImpl = mockFetch({ code: 'kb_not_found', message: '知识库不存在' }, 404)
    const client = createKbClient({ apiBaseUrl: '' }, fetchImpl)

    const failure = await client.listDocs('global', 'missing').catch((e) => e)

    expect(failure).toBeInstanceOf(RuntimeHttpError)
    expect(failure.status).toBe(404)
    expect(failure.code).toBe('kb_not_found')
  })

  it('uploads import-preview as multipart form data with a file field', async () => {
    const preview = {
      knowledge_base: 'tech_faq',
      filename: 'manual.md',
      media_type: 'markdown',
      chunks: [
        {
          index: 0,
          suggested_doc_id: 'doc-73c63791',
          suggested_title: '3.2 易损件保修范围',
          suggested_category: 'manual',
          content: '...',
          section_path: '保修政策/3.2 易损件保修范围',
          has_table: false,
          char_count: 432,
          duplicate_of: null,
        },
      ],
      warnings: [],
      stats: {
        chunk_count: 1,
        min_chars: 432,
        max_chars: 432,
        avg_chars: 432,
        broken_tables: 0,
        dropped_images: 0,
        cleaned_lines: 0,
      },
    }
    const fetchImpl = mockFetch(preview)
    const client = createKbClient({ apiBaseUrl: 'http://api' }, fetchImpl)

    const file = new File(['# 保修'], 'manual.md', { type: 'text/markdown' })
    const result = await client.importPreview('global', 'tech_faq', file)

    const [url, init] = fetchImpl.mock.calls[0]!
    expect(url).toBe(
      'http://api/v1/platform/knowledge-bases/global/tech_faq/docs/import-preview',
    )
    expect(init?.method).toBe('POST')
    // multipart 由 fetch 自动生成带边界的 Content-Type，客户端不得手动设置。
    expect(new Headers(init?.headers).get('Content-Type')).toBeNull()
    const formData = init?.body as FormData
    expect(formData).toBeInstanceOf(FormData)
    expect((formData.get('file') as File).name).toBe('manual.md')
    expect(result.stats.chunk_count).toBe(1)
    expect(result.chunks[0]?.suggested_doc_id).toBe('doc-73c63791')
  })

  it('surfaces FastAPI-style detail error bodies from import-preview', async () => {
    const fetchImpl = mockFetch({ detail: '不支持的文件类型' }, 415)
    const client = createKbClient({ apiBaseUrl: '' }, fetchImpl)

    const failure = await client
      .importPreview('global', 'tech_faq', new File(['x'], 'a.exe'))
      .catch((e) => e)

    expect(failure).toBeInstanceOf(RuntimeHttpError)
    expect(failure.status).toBe(415)
    expect(failure.message).toBe('不支持的文件类型')
  })

  it('posts import docs as JSON and tolerates an empty 201 body', async () => {
    const fetchImpl = vi
      .fn<typeof fetch>()
      .mockImplementation(() => Promise.resolve(new Response(null, { status: 201 })))
    const client = createKbClient({ apiBaseUrl: '' }, fetchImpl)
    const doc = {
      doc_id: 'doc-1',
      title: 't',
      category: 'c',
      content: 'body',
      updated_at: '2026-10-02',
    }

    await client.importDocs('global', 'tech_faq', [doc])

    const [url, init] = fetchImpl.mock.calls[0]!
    expect(url).toBe('/v1/platform/knowledge-bases/global/tech_faq/docs/import')
    expect(init?.method).toBe('POST')
    expect(JSON.parse(String(init?.body))).toEqual({ docs: [doc] })
  })

  it('surfaces 409 conflict messages from import', async () => {
    const fetchImpl = mockFetch(
      { code: 'doc_conflict', message: 'doc_id 冲突：doc-1、doc-2' },
      409,
    )
    const client = createKbClient({ apiBaseUrl: '' }, fetchImpl)

    const failure = await client.importDocs('global', 'tech_faq', []).catch((e) => e)

    expect(failure).toBeInstanceOf(RuntimeHttpError)
    expect(failure.status).toBe(409)
    expect(failure.message).toContain('doc-1')
  })
})

describe('parseKbSource', () => {
  it('parses global and agent-scoped sources', () => {
    expect(parseKbSource('pricing')).toEqual({ scope: 'global', name: 'pricing' })
    expect(parseKbSource('auto-after-sales/vip_policy')).toEqual({
      scope: 'auto-after-sales',
      name: 'vip_policy',
    })
  })
})

describe('parseBatchCsv', () => {
  it('parses header, doc_id column and quoted answer column', () => {
    const text = [
      'question,expected_doc_ids,reference_answer',
      'S4 A 保多少钱,pricing-007|pricing-021,"A 保 1400-1700 元, 约 1.5 小时"',
      '工时费怎么算,,',
      ',pricing-001,忽略空问题行',
    ].join('\n')

    const questions = parseBatchCsv(text)

    expect(questions).toHaveLength(2)
    expect(questions[0]).toEqual({
      question: 'S4 A 保多少钱',
      expected_doc_ids: ['pricing-007', 'pricing-021'],
      reference_answer: 'A 保 1400-1700 元, 约 1.5 小时',
    })
    expect(questions[1]).toEqual({ question: '工时费怎么算' })
  })

  it('parses headerless text with one question per line', () => {
    const questions = parseBatchCsv('A 保多少钱\nB 保多少钱\n')
    expect(questions.map((q) => q.question)).toEqual(['A 保多少钱', 'B 保多少钱'])
  })
})
