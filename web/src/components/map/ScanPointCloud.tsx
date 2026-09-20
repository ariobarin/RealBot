import { useEffect, useMemo, useState } from 'react'
import { BufferGeometry, Float32BufferAttribute, Uint8BufferAttribute } from 'three'
import type { PointCloudSummary } from '../../lib/manifest'

interface CloudMeta {
  count: number
  pointSize: number
}

interface LoadedCloud {
  positions: Float32Array
  colors: Uint8Array
  pointSize: number
}

/** Full-colour bracketbot voxel scan, stored compactly as float32 XYZ followed by RGB bytes. */
export function ScanPointCloud({ source }: { source: PointCloudSummary }) {
  const [cloud, setCloud] = useState<LoadedCloud | null>(null)

  useEffect(() => {
    const abort = new AbortController()

    Promise.all([
      fetch(source.meta, { signal: abort.signal }).then((res) => {
        if (!res.ok) throw new Error(`point-cloud metadata: HTTP ${res.status}`)
        return res.json() as Promise<CloudMeta>
      }),
      fetch(source.data, { signal: abort.signal }).then((res) => {
        if (!res.ok) throw new Error(`point cloud: HTTP ${res.status}`)
        return res.arrayBuffer()
      }),
    ])
      .then(([meta, buffer]) => {
        const positionBytes = meta.count * 3 * Float32Array.BYTES_PER_ELEMENT
        const colorBytes = meta.count * 3
        if (buffer.byteLength !== positionBytes + colorBytes) {
          throw new Error(`point cloud: expected ${positionBytes + colorBytes} bytes, got ${buffer.byteLength}`)
        }
        setCloud({
          positions: new Float32Array(buffer, 0, meta.count * 3),
          colors: new Uint8Array(buffer, positionBytes, colorBytes),
          pointSize: meta.pointSize,
        })
      })
      .catch((error: unknown) => {
        if (!abort.signal.aborted) console.error(error)
      })

    return () => {
      abort.abort()
    }
  }, [source])

  return cloud ? <ColoredPointCloud {...cloud} /> : null
}

export function ColoredPointCloud({ positions, colors, pointSize }: LoadedCloud) {
  const geometry = useMemo(() => {
    const result = new BufferGeometry()
    result.setAttribute('position', new Float32BufferAttribute(positions, 3))
    result.setAttribute('color', new Uint8BufferAttribute(colors, 3, true))
    result.computeBoundingSphere()
    return result
  }, [positions, colors])
  useEffect(() => () => geometry.dispose(), [geometry])
  return (
    <points geometry={geometry} name="scan-point-cloud">
      <pointsMaterial vertexColors size={pointSize} sizeAttenuation toneMapped={false} />
    </points>
  )
}
