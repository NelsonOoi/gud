/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_GUD_API?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
