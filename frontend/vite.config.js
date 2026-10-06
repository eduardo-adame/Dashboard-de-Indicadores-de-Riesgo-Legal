import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// El proxy conserva el prefijo y las cookies; nunca compila credenciales.
export function apiProxy(target = process.env.FRONTEND_API_TARGET || 'http://127.0.0.1:8000') {
  const url = new URL(target)
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.pathname !== '/' || url.search || url.hash) throw new Error('El origen de API no es válido.')
  return { target: url.origin, changeOrigin: false, timeout: 420000, proxyTimeout: 420000 }
}
export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    setupFiles: ['./tests/setup.js'],
    include: ['tests/**/*.test.{js,jsx}'],
    maxWorkers: 1,
  },
  server: {
    host: '127.0.0.1',
    port: 3000,
    strictPort: false,
    proxy: { '/api': apiProxy() },
  },
  preview: {
    host: '127.0.0.1',
    port: 3000,
    proxy: { '/api': apiProxy() },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    chunkSizeWarningLimit: 600,
  },
})
