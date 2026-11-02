import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 开发期把 /api 与 /health 代理到本地后端，生产构建则与后端同源部署。
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  server: {
    port: 5180,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8010', changeOrigin: false },
      '/health': { target: 'http://127.0.0.1:8010', changeOrigin: false },
    },
  },
  build: { outDir: 'dist', emptyOutDir: true },
})
