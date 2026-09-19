import { useEffect, useState } from 'react'
import { BufferGeometry, Float32BufferAttribute, Uint8BufferAttribute } from 'three'
import type { PointCloudSummary } from '../../lib/manifest'

interface CloudMeta {
  count: number
  pointSize: number
}

interface LoadedCloud {
  geometry: BufferGeometry
  pointSize: number
}

/** Full-colour bracketbot voxel scan, stored compactly as float32 XYZ followed by RGB bytes. */
export function ScanPointCloud({ source }: { source: PointCloudSummary }) {
  const [cloud, setCloud] = useState<LoadedCloud | null>(null)

  useEffect(() => {
    const abort = new AbortController()
    let loaded: LoadedCloud | null = null

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
        const geometry = new BufferGeometry()
        geometry.setAttribute('position', new Float32BufferAttribute(new Float32Array(buffer, 0, meta.count * 3), 3))
        geometry.setAttribute('color', new Uint8BufferAttribute(new Uint8Array(buffer, positionBytes, colorBytes), 3, true))
        geometry.computeBoundingSphere()
        loaded = { geometry, pointSize: meta.pointSize }
        setCloud(loaded)
      })
      .catch((error: unknown) => {
        if (!abort.signal.aborted) console.error(error)
      })

    return () => {
      abort.abort()
      loaded?.geometry.dispose()
    }
  }, [source])

  if (!cloud) return null
  return (
    <points geometry={cloud.geometry} name="scan-point-cloud">
      <pointsMaterial vertexColors size={cloud.pointSize} sizeAttenuation toneMapped={false} />
    </points>
  )
}
