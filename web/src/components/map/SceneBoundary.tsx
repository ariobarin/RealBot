import { Component, type ReactNode } from 'react'
import type { MapSummary } from '../../lib/manifest'

interface Props {
  summary: MapSummary
  children: ReactNode
}

interface State {
  failed: boolean
}

/** Falls back to the flat SLAM thumbnail when WebGL is unavailable (e.g. headless or GPU-less browsers). */
export class SceneBoundary extends Component<Props, State> {
  state: State = { failed: false }

  static getDerivedStateFromError(): State {
    return { failed: true }
  }

  componentDidCatch(err: unknown) {
    console.warn('[map] 3D scene unavailable, showing 2D fallback', err)
  }

  render() {
    if (!this.state.failed) return this.props.children
    return (
      <div data-testid="scene-fallback" className="absolute inset-0 grid place-items-center bg-bg-soft p-6">
        <img
          src={this.props.summary.thumb}
          alt={`${this.props.summary.name} SLAM floor plan`}
          className="max-h-full max-w-full rounded-2xl shadow-md"
        />
        <p className="absolute bottom-20 rounded-full bg-white/80 px-3 py-1 text-xs text-ink-2 shadow-sm backdrop-blur sm:bottom-6">
          3D view needs WebGL — showing the flat scan instead.
        </p>
      </div>
    )
  }
}
