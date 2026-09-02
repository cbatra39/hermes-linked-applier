import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

/**
 * Vite config for the Hermes dashboard.
 *
 * In production the app is built to `dist/` and served by nginx (see nginx.conf),
 * which proxies `/api` -> http://hermes-core:8080.
 *
 * In local dev (`npm run dev`) there is no nginx, so we replicate that proxy here
 * against a hermes-core reachable on the host (docker compose publishes 8080).
 * Override with HERMES_CORE_ORIGIN=http://localhost:8080 if you moved the port.
 */
const CORE_ORIGIN = process.env.HERMES_CORE_ORIGIN || 'http://127.0.0.1:8080';

export default defineConfig({
  plugins: [react()],
  server: {
    host: '0.0.0.0',
    port: 3000,
    strictPort: true,
    proxy: {
      '/api': {
        target: CORE_ORIGIN,
        changeOrigin: true,
        // Server-Sent Events must not be buffered or compressed by the dev proxy.
        ws: false,
        configure: (proxy) => {
          proxy.on('proxyReq', (proxyReq) => {
            proxyReq.setHeader('accept-encoding', 'identity');
          });
        },
      },
    },
  },
  preview: {
    host: '0.0.0.0',
    port: 3000,
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    // Keep the bundle in one predictable place; nginx caches /assets aggressively.
    assetsDir: 'assets',
    chunkSizeWarningLimit: 900,
  },
});
