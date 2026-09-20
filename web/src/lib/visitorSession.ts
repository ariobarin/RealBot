import { parseViewerSession } from './liveTelemetry'

let approvedCode = ''

export function visitorAccessCode() {
  return approvedCode
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
  if (roomId && data.roomId !== roomId) throw new Error('Robot session does not match the requested robot')
  if (!['mobile', 'act'].includes(data.robotRole)) throw new Error('Robot role is unavailable')
  approvedCode = accessCode
  return { ...session, roomId: data.roomId, robotRole: data.robotRole as 'mobile' | 'act' }
}
