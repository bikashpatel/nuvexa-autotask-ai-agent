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
    // Headroom for the heaviest page renders (Settings: ~100 Mantine inputs) under parallel-worker
    // load. Tests take well under 2 s locally; this bound only prevents CI flakiness.
    testTimeout: 15_000,
    coverage: {
      provider: 'v8',
      include: ['src/**/*.{ts,tsx}'],
      exclude: [
        // Tests and test-only helpers (render wrappers, fetch mock, fixtures, axe helper).
        'src/**/*.test.{ts,tsx}',
        'src/test/**',
        // Ambient type declarations carry no runtime code.
        'src/**/*.d.ts',
        // Bootstrap entry: imports global CSS and calls createRoot() on #root. It contains no logic
        // of its own — the providers (App.tsx), router and query client it wires are tested directly.
        'src/main.tsx',
      ],
      reporter: ['text', 'lcov', 'json-summary'],
      reportsDirectory: './coverage',
      // Achieved coverage rounded down (99.93 / 100 / 100 / 98.55). Any new uncovered code fails CI.
      thresholds: {
        statements: 99,
        lines: 100,
        functions: 100,
        branches: 98,
      },
    },
  },
});
