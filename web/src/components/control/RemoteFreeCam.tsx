import { useEffect, useRef, useState } from 'react'
import { Camera } from 'lucide-react'
import { VisitorLiveKit, type FreeCamState } from '../../lib/visitorLiveKit'

export function RemoteFreeCam({ client, state, onViewing }: {
  client: VisitorLiveKit | null
  state: FreeCamState | null
  onViewing: (viewing: boolean) => void
}) {
  const open = !!state?.viewing
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const [supported, setSupported] = useState(false)
  const direction = useRef([0, 0])
  const active = state?.phase === 'active' && state.ready
  const button = 'rounded-xl border border-line bg-white px-4 py-2 text-ink disabled:opacity-40 touch-none'

  async function command(action: string) {
    direction.current = [0, 0]
    setPending(true)
    setError('')
    try {
      if (!client) throw new Error('Connect to the robot first')
      if (action === 'open') onViewing(true)
      await client.freeCamCommand(action, supported)
      if (action === 'close') onViewing(false)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Free Cam command failed')
      if (action === 'open') onViewing(false)
    } finally { setPending(false) }
  }

  useEffect(() => {
    if (!open || !client) return
    const held = new Set<string>()
    const keys: Record<string, [number, number]> = {
      ArrowLeft: [-1, 0], a: [-1, 0], ArrowRight: [1, 0], d: [1, 0],
      ArrowUp: [0, 1], w: [0, 1], ArrowDown: [0, -1], s: [0, -1],
    }
    const stop = () => {
      held.clear()
      direction.current = [0, 0]
      void client.freeCamCommand('stop').catch(() => {})
    }
    const key = (event: KeyboardEvent) => {
      if (event.target instanceof HTMLInputElement) return
      if (event.key === 'Escape' || event.key === ' ') { event.preventDefault(); stop(); return }
      const name = event.key.length === 1 ? event.key.toLowerCase() : event.key
      if (!keys[name]) return
      event.preventDefault()
      if (event.type === 'keydown') held.add(name)
      else held.delete(name)
      const total = [...held].reduce((sum, k) => [sum[0] + keys[k][0], sum[1] + keys[k][1]], [0, 0])
      direction.current = total.map(Math.sign)
    }
    const hidden = () => { if (document.hidden) stop() }
    const pulse = setInterval(() => client.freeCamPulse(...direction.current as [number, number]), 100)
    window.addEventListener('keydown', key)
    window.addEventListener('keyup', key)
    window.addEventListener('blur', stop)
    document.addEventListener('visibilitychange', hidden)
    return () => {
      clearInterval(pulse)
      direction.current = [0, 0]
      window.removeEventListener('keydown', key)
      window.removeEventListener('keyup', key)
      window.removeEventListener('blur', stop)
      document.removeEventListener('visibilitychange', hidden)
      void client.freeCamCommand('close').catch(() => {})
    }
  }, [open, client])

  return <div className="flex flex-wrap items-center gap-2">
    <button className={button + ' flex items-center gap-2'} disabled={pending || (!open && !state?.available)}
      onClick={() => void command(open ? 'close' : 'open')}><Camera size={18} />{open ? 'Back to tour' : 'Free Cam'}</button>
    {open && <>
      <button className={button} disabled={pending || state?.phase === 'active' || state?.phase === 'starting'}
        onClick={() => void command('start')}>Start / resume</button>
      <button className={button} onClick={() => void command('stop')}>Stop / hold</button>
      {([['←', -1, 0], ['↑', 0, 1], ['↓', 0, -1], ['→', 1, 0]] as const).map(([label, pan, tilt]) =>
        <button key={label} className={button} disabled={!active || pending} aria-label={`Look ${label}`}
          onPointerDown={(event) => { event.currentTarget.setPointerCapture(event.pointerId); direction.current = [pan, tilt] }}
          onPointerUp={() => { direction.current = [0, 0] }}
          onPointerCancel={() => { direction.current = [0, 0] }}
          onLostPointerCapture={() => { direction.current = [0, 0] }}>{label}</button>)}
      <button className={button} disabled={!active || pending} onClick={() => void command('pose')}>Look forward</button>
      <button className={button} disabled={!active || pending} onClick={() => void command('open_gripper')}>Open claw</button>
      <label className="text-sm"><input type="checkbox" checked={supported} onChange={(e) => setSupported(e.target.checked)} /> Arm supported</label>
      <button className={button} disabled={!supported || pending || state?.phase === 'idle'} onClick={() => void command('release')}>Release torque</button>
      <p role="status" className="w-full text-sm">{state?.reason || state?.notice || (state?.moving ? 'Moving' : state?.phase)}
        {state?.offsets && ` · Pan ${Math.round(state.offsets[0])}° · Tilt ${Math.round(state.offsets[1])}°`}
        {' · Hold arrows or WASD to look around. Stop keeps the arm and base held.'}</p>
    </>}
    {error && <p role="alert" className="w-full text-sm text-red-700">{error}</p>}
  </div>
}
