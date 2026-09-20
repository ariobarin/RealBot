const { createHash, timingSafeEqual } = require('node:crypto')

module.exports = function session(request, response) {
  response.setHeader('Cache-Control', 'no-store')
  if (request.method !== 'POST') {
    response.setHeader('Allow', 'POST')
    return response.status(405).json({ error: 'Use POST' })
  }
  const expected = process.env.LIVEKIT_ACCESS_CODE_SHA256?.trim()
  if (!/^[a-f0-9]{64}$/.test(expected || '')) {
    return response.status(503).json({ error: 'Robot session unavailable' })
  }
  const code = request.body?.accessCode
  if (typeof code !== 'string' || code.length > 256 || !timingSafeEqual(
    createHash('sha256').update(code).digest(), Buffer.from(expected, 'hex'),
  )) return response.status(401).json({ error: 'Invalid access code' })
  try {
    const session = JSON.parse(process.env.LIVEKIT_VISITOR_SESSION)
    if (session.kind !== 'browser' || !Number.isFinite(session.expiresAt)
        || Date.now() >= session.expiresAt * 1000 || !session.token
        || !session.visitorRoomId || !session.robotIdentity
        || !session.url?.startsWith('wss://')) throw new Error('Unavailable')
    if (request.body.roomId !== session.visitorRoomId) {
      return response.status(403).json({ error: 'This code does not allow that robot' })
    }
    return response.status(200).json({
      url: session.url, token: session.token, robotIdentity: session.robotIdentity,
    })
  } catch {
    return response.status(503).json({ error: 'Robot session expired or unavailable' })
  }
}
