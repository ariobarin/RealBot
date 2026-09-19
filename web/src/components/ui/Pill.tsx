import type { ReactNode } from 'react'

type Tone = 'neutral' | 'ok' | 'brand'

const tones: Record<Tone, string> = {
  neutral: 'bg-bg-soft text-ink-2',
  ok: 'bg-[#e6f4e6] text-ok',
  brand: 'bg-brand-soft text-brand-2',
}

export function Pill({ tone = 'neutral', children }: { tone?: Tone; children: ReactNode }) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium ${tones[tone]}`}
    >
      {children}
    </span>
  )
}
