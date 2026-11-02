// CabinWeave 本地开发代理，将浏览器 API 请求转发到 Python 后端。
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      '/api/python': {
        target: process.env.VITE_PYTHON_API_URL || 'http://127.0.0.1:8000',
        changeOrigin: true,
        rewrite: path => path.replace(/^\/api\/python/, '')
      }
    }
  }
})
