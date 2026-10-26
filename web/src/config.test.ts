import { describe, expect, it } from 'vitest'

import { loadRuntimeConfig } from './config'

describe('loadRuntimeConfig', () => {
  it('loads only connection and public scope settings', () => {
    const config = loadRuntimeConfig({
      VITE_API_BASE_URL: 'https://runtime.example.com/',
      VITE_APP_ID: 'console',
      VITE_USER_ID: 'operator',
      VITE_AGENT_CATALOG: '[{"id":"stale"}]',
    })

    expect(config).toEqual({
      apiBaseUrl: 'https://runtime.example.com',
      appId: 'console',
      userId: 'operator',
    })
  })
})
