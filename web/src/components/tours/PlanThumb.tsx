/**
 * A real SLAM floor plan (the preset's rendered thumb) on the warm-paper well, with the
 * robot's position. Live tours get the pulsing coral dot, scheduled ones a parked grey one.
 */
export function PlanThumb({
  mapId,
  name,
  robot,
  live,
}: {
  mapId: string
  name: string
  robot: [number, number]
  live: boolean
}) {
  const [rx, ry] = robot
  return (
    <div className="relative size-full bg-[var(--map-floor)]">
      <img
        src={`/maps/${mapId}/thumb.png`}
        alt={`SLAM floor plan of ${name}`}
        loading="lazy"
        className="size-full object-contain p-3"
      />
      <span
        aria-hidden
        className="absolute"
        style={{ left: `${rx * 100}%`, top: `${ry * 100}%`, transform: 'translate(-50%, -50%)' }}
      >
        {live && <span className="orb-ring absolute -inset-1.5 rounded-full bg-brand/40" />}
        <span
          className={`relative block size-3 rounded-full ring-[2.5px] ring-white ${
            live ? 'bg-brand' : 'bg-ink-3'
          }`}
        />
      </span>
    </div>
  )
}
