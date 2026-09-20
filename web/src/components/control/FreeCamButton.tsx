import { useState } from 'react'
import { Camera, X } from 'lucide-react'
import { useParams } from 'react-router-dom'

export function FreeCamButton({ onOpen }: { onOpen: () => void }) {
  const { roomId } = useParams()
  const [url, setUrl] = useState('')
  const [error, setError] = useState('')
  const [pending, setPending] = useState(false)

  async function open() {
    setPending(true)
    setError('')
    try {
      const response = await fetch(`/api/freecam-session?roomId=${encodeURIComponent(roomId || '')}`, { cache: 'no-store', signal: AbortSignal.timeout(3000) })
      if (!response.ok) throw new Error('Free Cam is not connected for this tour yet.')
      const session = await response.json()
      const target = new URL(session.url)
      if (session.roomId !== roomId || target.origin !== 'http://127.0.0.1:8012' || target.pathname !== '/' || !target.hash)
        throw new Error('Free Cam connection is unavailable.')
      onOpen()
      setUrl(target.href)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Could not open Free Cam.')
    } finally {
      setPending(false)
    }
  }

  return <>
    <button type="button" disabled={pending} onClick={() => void open()}
      className="flex items-center gap-2 rounded-xl border border-line bg-white px-5 py-2 font-semibold text-ink disabled:opacity-40">
      <Camera size={18} /> {pending ? 'Opening…' : 'Free Cam'}
    </button>
    {error && <p role="alert" className="text-sm text-red-700">{error}</p>}
    {url && <dialog aria-label="Right-hand Free Cam" onCancel={() => setUrl('')}
      ref={(dialog) => { if (dialog && !dialog.open) dialog.showModal() }}
      className="fixed inset-0 m-0 flex h-dvh max-h-none w-screen max-w-none flex-col bg-[#182027] p-0 text-white">
      <header className="flex items-center justify-between border-b border-white/15 px-5 py-3">
        <span className="font-semibold">Right-hand Free Cam</span>
        <button type="button" onClick={() => setUrl('')} className="flex items-center gap-2 rounded-lg px-3 py-2 hover:bg-white/10">
          <X size={18} /> Back to tour
        </button>
      </header>
      <iframe title="Right-hand camera controls" src={url} referrerPolicy="no-referrer" className="min-h-0 w-full flex-1 border-0" />
    </dialog>}
  </>
}
