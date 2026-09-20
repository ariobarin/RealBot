export type KeyboardConnector = (ready: () => void, closed: (reason: string) => void) => {
  send: (command: { keys: string; shift: boolean }) => void
  close: () => void
}

const connectSshKeyboard: KeyboardConnector = (ready, closed) => {
  const socket = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/robot-camera/drive`)
  socket.onopen = () => socket.send(JSON.stringify({ keys: '', shift: false }))
  socket.onmessage = (event) => { if (JSON.parse(event.data).ready === true) ready() }
  socket.onclose = (event) => closed(event.reason)
  return {
    send: (command) => { if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(command)) },
    close: () => { socket.onopen = socket.onmessage = socket.onclose = null; socket.close() },
  }
}

export function startKeyboardDrive(status: (message: string) => void, stopped: () => void, connect = connectSshKeyboard) {
  const held = new Set<string>()
  let ready = false
  let shift = false
  const connection = connect(() => {
    ready = true
    status('WASD to move · Shift for faster · Space to stop')
  }, (reason) => {
    ready = false
    status(reason || 'Drive disconnected. Enable it again to resume.')
    stopped()
  })
  const send = () => {
    const vertical = held.has('w') === held.has('s') ? '' : held.has('w') ? 'w' : 's'
    const turn = held.has('a') === held.has('d') ? '' : held.has('a') ? 'a' : 'd'
    connection.send({ keys: vertical + turn, shift })
  }
  const stop = () => {
    held.clear()
    shift = false
    send()
    ready = false
    status('Drive stopped')
    stopped()
  }
  const key = (event: KeyboardEvent) => {
    if (!ready) return
    if (event.key === 'Escape' || event.code === 'Space') {
      event.preventDefault()
      stop()
      return
    }
    if (event.target instanceof HTMLElement && event.target.closest('input, textarea, select, [contenteditable="true"]')) return
    const name = event.key.toLowerCase()
    if (!['w', 'a', 's', 'd', 'shift'].includes(name)) return
    event.preventDefault()
    if (event.repeat) return
    shift = event.shiftKey
    if (event.type === 'keydown') held.add(name)
    else held.delete(name)
    send()
  }
  const visibility = () => { if (document.hidden) stop() }
  const heartbeat = window.setInterval(() => { if (ready) send() }, 50)
  window.addEventListener('keydown', key)
  window.addEventListener('keyup', key)
  window.addEventListener('blur', stop)
  document.addEventListener('visibilitychange', visibility)
  return () => {
    held.clear()
    shift = false
    send()
    ready = false
    clearInterval(heartbeat)
    window.removeEventListener('keydown', key)
    window.removeEventListener('keyup', key)
    window.removeEventListener('blur', stop)
    document.removeEventListener('visibilitychange', visibility)
    connection.close()
  }
}
