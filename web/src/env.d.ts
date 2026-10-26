/// <reference types="vite/client" />
/**
 * 前端可选构建时环境变量的编译期声明。
 */

interface ImportMetaEnv {
  readonly VITE_API_BASE_URL?: string
  readonly VITE_APP_ID?: string
  readonly VITE_USER_ID?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
