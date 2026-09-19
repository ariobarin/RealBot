import { ArrowLeft } from 'lucide-react'
import { Link, useParams } from 'react-router-dom'
import { PageShell, Wordmark } from '../components/ui/PageShell'
import { Pill } from '../components/ui/Pill'

/** Empty shells for routes that land in later sprints. */
export function PlaceholderPage({ title, sprint }: { title: string; sprint: number }) {
  const { mapId } = useParams()
  return (
    <PageShell>
      <header className="flex items-center justify-between">
        <Wordmark />
      </header>
      <div className="mt-24 flex flex-col items-center text-center">
        <Pill tone="brand">Sprint {sprint}</Pill>
        <h1 className="mt-4 text-3xl font-bold tracking-tight">{title}</h1>
        {mapId && <p className="mt-2 font-mono text-sm text-ink-2">{mapId}</p>}
        <p className="mt-3 max-w-sm text-ink-2">This screen is a placeholder for now.</p>
        <Link
          to="/"
          className="mt-8 inline-flex items-center gap-2 rounded-full border border-ink px-5 py-2.5 text-sm font-semibold text-ink no-underline transition-colors hover:bg-bg-soft"
        >
          <ArrowLeft size={16} /> Back to your spaces
        </Link>
      </div>
    </PageShell>
  )
}
