import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import PendingInputList from './PendingInputList.vue'

describe('PendingInputList', () => {
  it('summarizes queued text and media alongside steer inputs', () => {
    const wrapper = mount(PendingInputList, {
      props: {
        inputs: [
          {
            messageId: 'queue-1',
            role: 'user',
            origin: 'user',
            content: '下一轮再回答这个',
            blocks: [{ type: 'text', text: '下一轮再回答这个' }],
            mode: 'queue',
            createdAt: '2026-08-05T10:00:00Z',
          },
          {
            messageId: 'steer-1',
            role: 'system',
            origin: 'workflow.notice',
            content: '改成简短版',
            blocks: [{ type: 'text', text: '改成简短版' }],
            mode: 'steer',
            createdAt: '2026-08-05T10:00:01Z',
          },
          {
            messageId: 'asset-1',
            role: 'user',
            origin: 'user',
            content: '',
            blocks: [
              {
                type: 'document',
                source_type: 'asset',
                media_type: 'application/pdf',
                filename: 'report.pdf',
                asset_id: 'asset-1',
              },
            ],
            mode: 'queue',
            createdAt: '2026-08-05T10:00:00Z',
          },
        ],
      },
    })

    expect(wrapper.findAll('[data-pending-input]')).toHaveLength(3)
    expect(wrapper.text()).toContain('下一轮再回答这个')
    expect(wrapper.text()).toContain('改成简短版')
    expect(wrapper.findAll('[data-pending-input]')[2]?.text()).toContain('report.pdf')
    wrapper.unmount()
  })
})
