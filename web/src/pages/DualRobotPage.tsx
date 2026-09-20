import { Link } from 'react-router-dom'
import { PageShell } from '../components/ui/PageShell'
import { RobotCameraPanel } from './SshCameraPage'

export function DualRobotPage() {
  return <PageShell wide viewport>
    <header className="mb-4 flex items-center justify-between">
      <h1 className="text-2xl font-semibold">Both robots</h1>
      <Link to="/" className="text-sm underline">Leave demo</Link>
    </header>
    <div className="grid min-h-0 flex-1 grid-cols-1 gap-5 overflow-auto lg:grid-cols-2">
      {['0187', '0188'].map((id) => <div key={id} className="flex min-h-[34rem] flex-col rounded-2xl border border-line p-4">
        <RobotCameraPanel robotId={id} embedded />
      </div>)}
    </div>
  </PageShell>
}
