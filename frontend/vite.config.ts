import { fileURLToPath, URL } from 'node:url'
import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'

// 开发代理可指定同版本后端；生产构建仍与后端同源部署。
export default defineConfig(({ mode }) => {
  const frontendRoot = fileURLToPath(new URL('.', import.meta.url))
  const backend = loadEnv(mode, frontendRoot, 'API_AGENT_').API_AGENT_DEV_BACKEND || 'http://127.0.0.1:8010'
  return {
    plugins: [vue()],
    resolve: {
      alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
    },
    server: {
      port: 5180,
      proxy: {
        '/api': { target: backend, changeOrigin: false },
        '/health': { target: backend, changeOrigin: false },
      },
    },
    build: { outDir: 'dist', emptyOutDir: true },
  }
})
