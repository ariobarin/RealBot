import { parseViewerSession } from './liveTelemetry'

let approved: { code: string; roomId: string } | undefined

export function visitorAccessCode(roomId: string | undefined) {
  return approved && approved.roomId === roomId ? approved.code : ''
}

export async function requestVisitorSession(accessCode: string, roomId?: string, signal?: AbortSignal) {
  const response = await fetch('/api/livekit-session', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, cache: 'no-store', signal,
    body: JSON.stringify({ accessCode, roomId }),
  })
  const data = await response.json().catch(() => null)
  if (!response.ok) throw new Error(data?.error || 'Robot session unavailable', { cause: response.status })
  const session = parseViewerSession(data)
  if (typeof data.roomId !== 'string' || !data.roomId) throw new Error('Robot session unavailable')
  approved = { code: accessCode, roomId: data.roomId }
  return { ...session, roomId: data.roomId }
}
