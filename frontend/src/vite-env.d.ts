/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_USE_MOCK?: string
  readonly VITE_API_URL?: string
  /** 'true' | 'false' | comma-separated Api method names */
  readonly VITE_MOCK_FALLBACK?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
