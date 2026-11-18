import { defineConfig } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

// Build: relative asset paths, so the UI works under any (random) ADMIN_PREFIX.
// Dev: served under /admin/; API calls proxied to the gateway with the Host header kept (host allowlist).
export default defineConfig(({ command }) => ({
  plugins: [svelte()],
  base: command === 'build' ? './' : '/admin/',
  build: { outDir: 'dist', assetsDir: 'assets', sourcemap: false },
  server: {
    host: '0.0.0.0',
    port: 38082,
    strictPort: true,
    proxy: { '/admin/api': { target: process.env.VITE_API_PROXY_TARGET || 'http://127.0.0.1:38081', changeOrigin: false } },
  },
}));
