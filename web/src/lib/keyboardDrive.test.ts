// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { startKeyboardDrive } from './keyboardDrive'

class Socket {
  static OPEN = 1
  readyState = 1
  onopen: (() => void) | null = null
  onmessage: ((event: { data: string }) => void) | null = null
  onclose: ((event: { reason: string }) => void) | null = null
  send = vi.fn()
  close = vi.fn()
}

let cleanup: (() => void) | undefined
afterEach(() => { cleanup?.(); vi.unstubAllGlobals(); vi.useRealTimers() })

function setup() {
  vi.useFakeTimers()
  const socket = new Socket()
  vi.stubGlobal('WebSocket', Object.assign(vi.fn(function () { return socket }), { OPEN: 1 }))
  const status = vi.fn()
  const stopped = vi.fn()
  cleanup = startKeyboardDrive(status, stopped)
  socket.onopen?.()
  socket.onmessage?.({ data: '{"ready":true}' })
  return { socket, stopped }
}

function key(type: string, name: string, shiftKey = false) {
  window.dispatchEvent(new KeyboardEvent(type, { key: name, shiftKey, cancelable: true }))
}

it('refreshes held keys and immediately stops on release, including Shift and opposing keys', () => {
  const { socket } = setup()
  key('keydown', 'w')
  vi.advanceTimersByTime(120)
  expect(socket.send).toHaveBeenLastCalledWith('{"keys":"w","shift":false}')
  key('keydown', 'Shift', true)
  expect(socket.send).toHaveBeenLastCalledWith('{"keys":"w","shift":true}')
  key('keydown', 's', true)
  expect(socket.send).toHaveBeenLastCalledWith('{"keys":"","shift":true}')
  key('keyup', 's')
  key('keyup', 'w')
  expect(socket.send).toHaveBeenLastCalledWith('{"keys":"","shift":false}')
})

it.each(['blur', 'hidden', 'Escape', 'disconnect'])('does not resume held keys after %s', (reason) => {
  const { socket, stopped } = setup()
  key('keydown', 'w')
  if (reason === 'blur') window.dispatchEvent(new Event('blur'))
  if (reason === 'hidden') {
    vi.spyOn(document, 'hidden', 'get').mockReturnValueOnce(true)
    document.dispatchEvent(new Event('visibilitychange'))
  }
  if (reason === 'Escape') key('keydown', 'Escape')
  if (reason === 'disconnect') socket.onclose?.({ reason: '' })
  expect(stopped).toHaveBeenCalled()
  const count = socket.send.mock.calls.length
  key('keydown', 'w')
  vi.advanceTimersByTime(1000)
  expect(socket.send).toHaveBeenCalledTimes(count)
})

it('releases control on leaving the page', () => {
  const { socket } = setup()
  key('keydown', 'w')
  cleanup?.()
  cleanup = undefined
  expect(socket.send).toHaveBeenLastCalledWith('{"keys":"","shift":false}')
  expect(socket.close).toHaveBeenCalledOnce()
  const count = socket.send.mock.calls.length
  vi.advanceTimersByTime(500)
  key('keydown', 'w')
  expect(socket.send).toHaveBeenCalledTimes(count)
})
