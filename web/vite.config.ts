import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig, loadEnv } from 'vite'
import { readFile } from 'node:fs/promises'
import { freeCamSession } from './freeCamSession.js'

export default defineConfig(({ mode }) => {
  const env = { ...loadEnv(mode, process.cwd(), ''), ...process.env }
  const target = env.ACTION_POINTS_TARGET
  const room = env.ACTION_POINTS_ROOM_ID
  const prefix = '/api/action-points/' + encodeURIComponent(room || '')
  return {
  plugins: [react(), tailwindcss(), freeCamSession(), {
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
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy: {
      ...(target && room ? { '/api/action-points/': {
        target,
        rewrite: (path: string) => [prefix + '/actions', prefix + '/stream'].includes(path) ? path.slice(prefix.length) : '/not-found',
      }} : {}),
      '/robot-camera': {
        target: 'http://127.0.0.1:18006',
        ws: true,
        rewrite: (path) => path.replace(/^\/robot-camera/, ''),
      },
    },
  },
  }
})
