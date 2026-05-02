import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const apiPort = env.VITE_API_PORT || '2333'
  return {
    plugins: [react()],
    server: {
      allowedHosts: true,
      proxy: {
        '/api': `http://localhost:${apiPort}`,
        '/ws': {
          target: `ws://localhost:${apiPort}`,
          ws: true,
        },
      },
    },
  }
})
