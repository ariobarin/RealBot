import { useEffect, useRef } from 'react'
import type { RemoteVideoTrack } from 'livekit-client'

export function ArmCameraPopup({ track, fresh, onClose, onStop }: {
  track?: RemoteVideoTrack; fresh: boolean; onClose: () => void; onStop: () => void
}) {
  const video = useRef<HTMLVideoElement>(null)
  useEffect(() => {
    const element = video.current
    if (!track || !element || !fresh) return
    track.attach(element)
    return () => { track.detach(element); element.srcObject = null }
  }, [track, fresh])

  return <section role="dialog" aria-label="Right-arm camera"
    className="absolute right-3 top-3 z-30 w-[min(28rem,calc(100%-1.5rem))] overflow-hidden rounded-xl border border-white/30 bg-black text-white shadow-xl">
    <header className="flex items-center justify-between gap-3 px-3 py-2 text-sm">
      <h2 className="font-semibold">Right-arm camera</h2>
      <button aria-label="Close right-arm camera" onClick={onClose} className="rounded-lg border px-2 py-1">Close</button>
    </header>
    <div className="relative aspect-[4/3]">
      {track && fresh ? <video ref={video} autoPlay playsInline muted
        aria-label="Live right-arm camera" className="h-full w-full object-contain" />
        : <p role="status" className="absolute inset-0 grid place-items-center p-4 text-center text-sm">Right-arm camera unavailable. Waiting for live video…</p>}
    </div>
    <footer className="flex items-center justify-between gap-3 px-3 py-2 text-sm">
      <span>Closing this view does not stop ACT.</span>
      <button onClick={onStop} className="shrink-0 rounded-lg bg-red-700 px-3 py-2">Stop / hold</button>
    </footer>
  </section>
}
