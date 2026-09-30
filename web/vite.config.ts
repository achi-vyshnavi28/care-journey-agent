/// <reference types="vitest/config" />
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// In development, /api is proxied to the FastAPI service (python scripts/demo_api.py, port 8930).
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5176,
    proxy: { '/api': { target: 'http://127.0.0.1:8930', rewrite: (p) => p.replace(/^\/api/, '') } },
  },
  test: { environment: 'jsdom', setupFiles: ['./src/setupTests.ts'] },
})
