import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'
import { readFile } from 'node:fs/promises'

export default defineConfig({
  plugins: [react(), tailwindcss(), {
    name: 'private-livekit-demo-session',
    configureServer(server) {
      server.middlewares.use('/api/livekit-session', async (request, response) => {
        if (request.method !== 'GET' || !['127.0.0.1:5178', 'localhost:5178'].includes(request.headers.host || '')) {
          response.writeHead(403).end(); return
        }
        try {
          const session = JSON.parse(await readFile(new URL('../.realbot-demo/current/browser.session.json', import.meta.url), 'utf8'))
          if (Date.now() >= session.expiresAt * 1000) throw new Error('Expired')
          response.writeHead(200, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' })
          response.end(JSON.stringify(session))
        } catch { response.writeHead(503).end('Session unavailable') }
      })
    },
  }],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      '/robot-camera': {
        target: 'http://127.0.0.1:18006',
        ws: true,
        rewrite: (path) => path.replace(/^\/robot-camera/, ''),
      },
    },
  },
})
