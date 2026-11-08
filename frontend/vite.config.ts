import { fileURLToPath, URL } from 'node:url'
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  // the in-app user guide (/help) bundles ../docs/guide (Markdown chapters + screenshots) via import.meta.glob
  server: { port: 5173, strictPort: false, fs: { allow: ['.', fileURLToPath(new URL('../docs/guide', import.meta.url))] } },
  preview: { port: 4173 },
  build: {
    target: 'es2022',
    sourcemap: false,
    chunkSizeWarningLimit: 1600,
  },
})
