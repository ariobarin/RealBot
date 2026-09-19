import { ArrowLeft, Radio, Wifi } from 'lucide-react'
import { useState, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { Button } from '../components/ui/Button'
import { PageShell, Wordmark } from '../components/ui/PageShell'

const savedRoom = () => localStorage.getItem('realbot-room') || 'demo-bot'

export function ConnectPage() {
  const [roomId, setRoomId] = useState(savedRoom)
  const navigate = useNavigate()

  const submit = (event: FormEvent) => {
    event.preventDefault()
    const room = roomId.trim()
    if (!room) return
    localStorage.setItem('realbot-room', room)
    void navigate(`/control/${encodeURIComponent(room)}`)
  }

  return (
    <PageShell>
      <header className="flex items-center justify-between">
        <Wordmark />
        <Link to="/" className="inline-flex items-center gap-1.5 text-sm text-ink-2 no-underline">
          <ArrowLeft size={16} /> Your spaces
        </Link>
      </header>

      <section className="mx-auto mt-16 max-w-xl rounded-3xl border border-line bg-white p-8 shadow-md sm:p-10">
        <div className="grid size-12 place-items-center rounded-2xl bg-brand-soft text-brand">
          <Radio size={24} />
        </div>
        <h1 className="mt-6 text-3xl font-bold tracking-tight">Connect to your bracketbot</h1>
        <p className="mt-2 text-ink-2">
          Join the same demo room as the robot relay. This hackathon connection does not use an account.
        </p>

        <form className="mt-8" onSubmit={submit}>
          <label htmlFor="room-id" className="text-sm font-semibold">
            Demo room ID
          </label>
          <input
            id="room-id"
            value={roomId}
            onChange={(event) => setRoomId(event.target.value)}
            placeholder="demo-bot"
            autoComplete="off"
            className="mt-2 w-full rounded-2xl border border-line bg-bg-soft px-4 py-3 text-ink outline-none focus:border-ink focus:ring-2 focus:ring-ink/10"
          />
          <Button type="submit" className="mt-5 w-full justify-center" disabled={!roomId.trim()}>
            <Wifi size={17} /> Connect
          </Button>
        </form>
      </section>
    </PageShell>
  )
}
