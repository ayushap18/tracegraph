import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const target = 'http://localhost:8777'

export default defineConfig({
  base: '/',
  plugins: [react()],
  build: { outDir: 'dist', emptyOutDir: true },
  server: {
    proxy: {
      // SSE: ask the upstream not to compress and flush headers straight through so events aren't held back.
      '/events': {
        target, changeOrigin: true,
        headers: { 'Accept-Encoding': 'identity' },
        configure: proxy => {
          proxy.on('proxyRes', res => { res.headers['cache-control'] = 'no-cache'; res.headers['x-accel-buffering'] = 'no' })
        },
      },
      '/ask': { target, changeOrigin: true },
      '/control': { target, changeOrigin: true },
      '/api': { target, changeOrigin: true },
    },
  },
})
