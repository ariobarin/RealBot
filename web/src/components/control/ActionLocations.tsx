import { useEffect } from 'react'
import type { ActionView, VisitorLiveKit } from '../../lib/visitorLiveKit'

export function ActionLocations({ client, view, disabled }: {
  client: VisitorLiveKit; view: ActionView | null; disabled: boolean
}) {
  const busy = view?.state.phase === 'loading' || view?.state.phase === 'running'

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

  return <>
    {!disabled && view?.points.map(point => <span key={point.id}
      aria-label={point.label} title={point.label}
      className="pointer-events-none absolute z-10 h-10 w-10 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-white bg-white/10"
      style={{ left: `${point.x * 100}%`, top: `${point.y * 100}%` }} />)}
    {(busy || view?.state.owned) && <div className="absolute left-3 top-3 z-20 flex max-w-[90%] items-center gap-3 rounded-xl bg-black/75 p-3 text-sm text-white">
      <span role="status">{view?.state.phase === 'running' ? 'Opening electrical box'
        : view?.state.reason || 'Stopped; arms held'}</span>
      <button className="shrink-0 rounded-lg border px-3 py-2" onClick={() => client.stopAction()}>Stop action</button>
    </div>}
  </>
}
