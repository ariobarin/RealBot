const { test } = require('node:test')
const assert = require('node:assert/strict')
const { createHash } = require('node:crypto')
const handler = require('../api/livekit-session.js')

test('only an authorized unexpired robot session releases its scoped token', () => {
  process.env.LIVEKIT_ACCESS_CODE_SHA256 = createHash('sha256').update('private-test-code').digest('hex')
  const session = { kind: 'browser', visitorRoomId: '0188', expiresAt: Date.now() / 1000 + 60,
    url: 'wss://example.livekit.cloud', token: 'scoped-test-token', robotIdentity: 'robot' }
  const call = (body, method = 'POST') => {
    const result = { headers: {}, setHeader(k, v) { this.headers[k] = v },
      status(n) { this.code = n; return this }, json(body) { this.body = body; return this } }
    handler({ method, body }, result)
    assert.equal(result.headers['Cache-Control'], 'no-store')
    return result
  }
  process.env.LIVEKIT_VISITOR_SESSION = JSON.stringify(session)
  assert.equal(call({}, 'GET').code, 405)
  for (const accessCode of [undefined, '', 'incorrect', ['private-test-code']]) {
    const response = call({ accessCode, roomId: '0188' })
    assert.equal(response.code, 401)
    assert.ok(!JSON.stringify(response.body).includes(session.token))
  }
  const request = { accessCode: 'private-test-code', roomId: '0188' }
  assert.equal(call({ ...request, roomId: '0187' }).code, 403)
  assert.deepEqual(call(request).body, {
    url: session.url, token: session.token, robotIdentity: session.robotIdentity, roomId: '0188', robotRole: 'mobile',
  })
  assert.equal(call({ accessCode: request.accessCode }).body.roomId, '0188')
  for (const invalid of ['{}', 'invalid json', JSON.stringify({ ...session, expiresAt: 1 })]) {
    process.env.LIVEKIT_VISITOR_SESSION = invalid
    assert.equal(call(request).code, 503)
  }
  delete process.env.LIVEKIT_ACCESS_CODE_SHA256
  assert.equal(call(request).code, 503)
  delete process.env.LIVEKIT_VISITOR_SESSION
})

test('two robots have isolated rooms and expiry, with no cross-robot token fallback', () => {
  process.env.LIVEKIT_ACCESS_CODE_SHA256 = createHash('sha256').update('two-bots').digest('hex')
  const sessions = ['0187', '0188'].map((id) => ({ kind: 'browser', visitorRoomId: id, roomId: `room-${id}`,
    expiresAt: Date.now()/1000 + 60, url: 'wss://example.livekit.cloud', token: `token-${id}`,
    robotIdentity: 'robot', robotRole: id === '0187' ? 'mobile' : 'act' }))
  const call = (id, code = 'two-bots') => {
    const result = { setHeader() {}, status(code) { this.code = code; return this }, json(body) { this.body = body; return this } }
    handler({ method: 'POST', body: { accessCode: code, roomId: id } }, result)
    return result
  }
  try {
    process.env.LIVEKIT_VISITOR_SESSIONS = JSON.stringify(sessions)
    for (const id of ['0187', '0188', '0187', '0188']) {
      const result = call(id)
      assert.equal(result.code, 200)
      assert.equal(result.body.token, `token-${id}`)
      assert.equal(result.body.roomId, id)
      assert.equal(result.body.robotRole, id === '0187' ? 'mobile' : 'act')
    }
    assert.equal(call('0188', 'wrong-code').code, 401)
    assert.equal(call('0189').code, 403)
    sessions[1].expiresAt = 1
    process.env.LIVEKIT_VISITOR_SESSIONS = JSON.stringify(sessions)
    assert.equal(call('0188').code, 503)
    assert.equal(call('0187').code, 200)
    sessions[1].roomId = sessions[0].roomId
    process.env.LIVEKIT_VISITOR_SESSIONS = JSON.stringify(sessions)
    assert.equal(call('0187').code, 503)
  } finally {
    delete process.env.LIVEKIT_ACCESS_CODE_SHA256
    delete process.env.LIVEKIT_VISITOR_SESSIONS
  }
})
