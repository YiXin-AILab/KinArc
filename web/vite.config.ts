import vue from '@vitejs/plugin-vue'
import { loadEnv } from 'vite'
import { defineConfig } from 'vitest/config'

export default defineConfig(({ mode }) => {
  const environment = loadEnv(mode, process.cwd(), '')
  return {
    plugins: [vue()],
    server: {
      port: 10061,
      proxy: {
        '/v1': {
          target: environment.DEV_PROXY_TARGET || 'http://127.0.0.1:10060',
          changeOrigin: true,
        },
      },
    },
    preview: {
      port: 10061,
    },
    test: {
      environment: 'jsdom',
      globals: true,
    },
  }
})
