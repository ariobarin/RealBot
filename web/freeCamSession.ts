import { readFile } from 'node:fs/promises'
import type { Plugin } from 'vite'

export function freeCamSession(): Plugin {
  return {
    name: 'private-freecam-session',
    configureServer(server) {
      server.middlewares.use('/api/freecam-session', async (request, response) => {
        const host = request.headers.host || ''
        const origin = request.headers.origin
        if (request.method !== 'GET' || !/^(127\.0\.0\.1|localhost):\d+$/.test(host)
          || request.headers['sec-fetch-site'] === 'cross-site' || (origin && origin !== `http://${host}`)) {
          response.writeHead(403).end(); return
        }
        try {
          const session = JSON.parse(await readFile(new URL('../.realbot-demo/freecam.session.json', import.meta.url), 'utf8'))
          const room = new URL(request.url || '/', 'http://localhost').searchParams.get('roomId')
          if (!room || room !== session.roomId) { response.writeHead(404).end('Free Cam unavailable for this tour'); return }
          const url = new URL(session.url)
          if (url.origin !== 'http://127.0.0.1:8012' || url.pathname !== '/' || !url.hash) throw new Error('Invalid session')
          response.writeHead(200, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' })
          response.end(JSON.stringify({ url: url.href, roomId: session.roomId }))
        } catch { response.writeHead(503).end('Free Cam unavailable') }
      })
    },
  }
}
