import { createPlatformClient, type PlatformClient } from '../api/platformClient'
import { loadRuntimeConfig } from '../config'

/** 页面默认使用的平台客户端；测试中 mock 本模块即可替换。 */
export function defaultPlatformClient(): PlatformClient {
  const config = loadRuntimeConfig(import.meta.env)
  return createPlatformClient({
    apiBaseUrl: config.apiBaseUrl,
    tokenProvider: () => sessionStorage.getItem('agentsystem.auth-token'),
  })
}
