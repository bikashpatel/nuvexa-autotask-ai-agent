import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

// The API is same-origin in every environment: Vite proxies /api in development and nginx
// proxies it in the container image, so the session cookie never crosses origins (no CORS).
const API_TARGET = process.env.NUVEXA_API_PROXY_TARGET ?? 'http://localhost:8000';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': {
        target: API_TARGET,
        changeOrigin: false,
        xfwd: true,
      },
    },
  },
  preview: {
    port: 4173,
    strictPort: true,
    proxy: {
      '/api': { target: API_TARGET, changeOrigin: false, xfwd: true },
    },
  },
  build: {
    target: 'es2023',
    sourcemap: false,
    chunkSizeWarningLimit: 900,
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
    css: false,
    restoreMocks: true,
    clearMocks: true,
  },
});
