module.exports = function session(request, response) {
  response.setHeader('Cache-Control', 'no-store')
  if (request.method !== 'POST') {
    response.setHeader('Allow', 'POST')
    return response.status(405).json({ error: 'Use POST' })
  }
  const code = request.body?.accessCode
  if (typeof code !== 'string' || !code || code.length > 256) {
    return response.status(401).json({ error: 'Invalid access code' })
  }
  try {
    const sessions = process.env.LIVEKIT_VISITOR_SESSIONS
      ? JSON.parse(process.env.LIVEKIT_VISITOR_SESSIONS)
      : [JSON.parse(process.env.LIVEKIT_VISITOR_SESSION)]
    if (!Array.isArray(sessions) || !sessions.length
        || sessions.some((s) => !s || typeof s.visitorRoomId !== 'string' || !s.visitorRoomId)
        || new Set(sessions.map((s) => s.visitorRoomId)).size !== sessions.length
        || new Set(sessions.map((s) => `${s.url}/${s.roomId}`)).size !== sessions.length) throw new Error('Invalid robot registry')
    const session = sessions.find((s) => s.visitorRoomId === code)
    if (!session) return response.status(401).json({ error: 'Invalid access code' })
    if (request.body.roomId !== undefined && request.body.roomId !== session.visitorRoomId) {
      return response.status(403).json({ error: 'This code does not allow that robot' })
    }
    if (session.kind !== 'browser' || !Number.isFinite(session.expiresAt)
        || Date.now() >= session.expiresAt * 1000 || !session.token
        || !session.visitorRoomId || !session.robotIdentity
        || !session.url?.startsWith('wss://')) throw new Error('Unavailable')
    const role = session.robotRole || 'mobile'
    if (!['mobile', 'act'].includes(role)) throw new Error('Invalid robot role')
    return response.status(200).json({
      url: session.url, token: session.token, robotIdentity: session.robotIdentity, roomId: session.visitorRoomId,
      robotRole: role,
    })
  } catch {
    return response.status(503).json({ error: 'Robot session expired or unavailable' })
  }
}
