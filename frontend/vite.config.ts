import {defineConfig} from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

// Point the dev proxy at another API with VITE_API_TARGET (default :8000).
const apiTarget = process.env.VITE_API_TARGET ?? 'http://localhost:8000';

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    // Fixed port so this app never collides with other Vite projects on
    // 5173. strictPort fails loudly instead of silently picking another one.
    port: 5180,
    strictPort: true,
    proxy: {
      '/health': apiTarget,
      '/api': {
        target: apiTarget,
        rewrite: path => path.replace(/^\/api/, ''),
      },
    },
  },
});
