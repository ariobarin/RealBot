import { useEffect, useRef, useState } from 'react'
import type { ActionView, VisitorLiveKit } from '../../lib/visitorLiveKit'

export function ActionLocations({ client, view, disabled, onStart }: {
  client: VisitorLiveKit; view: ActionView | null; disabled: boolean; onStart: () => void
}) {
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const submitting = useRef(false)
  const busy = pending || view?.state.phase === 'loading' || view?.state.phase === 'running'

  useEffect(() => {
    const stop = () => client.stopAction()
    const hidden = () => { if (document.hidden) stop() }
    const key = (event: KeyboardEvent) => { if (event.key === 'Escape') stop() }
    window.addEventListener('blur', stop)
    window.addEventListener('keydown', key)
    document.addEventListener('visibilitychange', hidden)
    return () => {
      stop()
      window.removeEventListener('blur', stop)
      window.removeEventListener('keydown', key)
      document.removeEventListener('visibilitychange', hidden)
    }
  }, [client])

  async function start(id: string) {
    if (submitting.current || busy) return
    submitting.current = true
    setPending(true)
    setError('')
    onStart()
    try { await client.startAction(id) }
    catch (cause) { setError(cause instanceof Error ? cause.message : 'Action could not start') }
    finally { submitting.current = false; setPending(false) }
  }

  return <>
    {!disabled && view?.points.map(point => <button key={point.id}
      aria-label={`Run ${point.label}`} title={point.action_id === 'electric_box'
        ? 'Open electrical box from the current arm pose. Position the robot at the box first.' : 'No trained policy for this action'}
      disabled={busy || !view.state.available || point.action_id !== 'electric_box'}
      className="absolute z-10 h-10 w-10 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-white bg-white/10 hover:bg-white/40 focus:outline-2 focus:outline-white disabled:cursor-not-allowed"
      style={{ left: `${point.x * 100}%`, top: `${point.y * 100}%` }} onClick={() => void start(point.id)} />)}
    {(busy || view?.state.owned || error) && <div className="absolute left-3 top-3 z-20 flex max-w-[90%] items-center gap-3 rounded-xl bg-black/75 p-3 text-sm text-white">
      <span role={error ? 'alert' : 'status'}>{error || (pending ? 'Starting electrical-box action…'
        : view?.state.phase === 'running' ? 'Opening electrical box'
          : view?.state.reason || 'Stopped; arms held')}</span>
      <button className="shrink-0 rounded-lg border px-3 py-2" onClick={() => client.stopAction()}>Stop action</button>
    </div>}
  </>
}
