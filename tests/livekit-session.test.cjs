const { test } = require('node:test')
const assert = require('node:assert/strict')
const handler = require('../api/livekit-session.js')

function call(accessCode, roomId, method = 'POST') {
  const result = { headers: {}, setHeader(k, v) { this.headers[k] = v },
    status(code) { this.code = code; return this }, json(body) { this.body = body; return this } }
  handler({ method, body: { accessCode, roomId } }, result)
  assert.equal(result.headers['Cache-Control'], 'no-store')
  return result
}

test('robot codes select isolated sessions and cannot authorize another robot', () => {
  const sessions = ['0187', '0188'].map((id) => ({ kind: 'browser', visitorRoomId: id, roomId: `room-${id}`,
    expiresAt: Date.now()/1000 + 60, url: 'wss://example.livekit.cloud', token: `token-${id}`,
    robotIdentity: 'robot', robotRole: id === '0187' ? 'mobile' : 'act' }))
  try {
    process.env.LIVEKIT_VISITOR_SESSIONS = JSON.stringify(sessions)
    assert.equal(call(undefined, undefined, 'GET').code, 405)
    for (const id of ['0187', '0188', '0187', '0188']) {
      for (const requested of [undefined, id]) {
        const result = call(id, requested)
        assert.equal(result.code, 200)
        assert.deepEqual(result.body, { url: sessions[0].url, token: `token-${id}`,
          robotIdentity: 'robot', roomId: id, robotRole: id === '0187' ? 'mobile' : 'act' })
      }
    }
    for (const code of [undefined, '', '0189', '187', 'old-shared-code', ['0187']]) {
      const result = call(code, '0187')
      assert.equal(result.code, 401)
      assert.ok(!JSON.stringify(result.body).includes('token-'))
    }
    assert.equal(call('0187', '0188').code, 403)
    assert.equal(call('0188', '0187').code, 403)
    sessions[1].expiresAt = 1
    process.env.LIVEKIT_VISITOR_SESSIONS = JSON.stringify(sessions)
    assert.equal(call('0188').code, 503)
    assert.equal(call('0187').code, 200)
    sessions[1].roomId = sessions[0].roomId
    process.env.LIVEKIT_VISITOR_SESSIONS = JSON.stringify(sessions)
    assert.equal(call('0187').code, 503)
  } finally { delete process.env.LIVEKIT_VISITOR_SESSIONS }
})

test('singular configuration requires its robot code and fails closed when unavailable', () => {
  const session = { kind: 'browser', visitorRoomId: '0188', expiresAt: Date.now()/1000 + 60,
    url: 'wss://example.livekit.cloud', token: 'scoped-test-token', robotIdentity: 'robot' }
  try {
    process.env.LIVEKIT_VISITOR_SESSION = JSON.stringify(session)
    assert.equal(call('0188').body.robotRole, 'mobile')
    assert.equal(call('0187').code, 401)
    for (const invalid of ['{}', 'invalid json', JSON.stringify({ ...session, expiresAt: 1 })]) {
      process.env.LIVEKIT_VISITOR_SESSION = invalid
      assert.equal(call('0188').code, 503)
    }
    delete process.env.LIVEKIT_VISITOR_SESSION
    assert.equal(call('0188').code, 503)
  } finally { delete process.env.LIVEKIT_VISITOR_SESSION }
})
