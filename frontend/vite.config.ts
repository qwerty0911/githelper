import {defineConfig} from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

// Point the dev proxy at another API with VITE_API_TARGET (default :8000).
const apiTarget = process.env.VITE_API_TARGET ?? 'http://localhost:8000';

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      '/health': apiTarget,
      '/api': {
        target: apiTarget,
        rewrite: path => path.replace(/^\/api/, ''),
      },
    },
  },
});
