export interface MapSnapshot {
  positions: number[]
  colors: number[]
  pointSize: number
  totalPoints: number
  robot: { x: number; y: number; heading: number }
}

export function mapCloud(snapshot: MapSnapshot) {
  const positions: number[] = []
  const colors: number[] = []
  for (let i = 0; i < snapshot.positions.length; i += 3) {
    const point = snapshot.positions.slice(i, i + 3)
    if (point.length !== 3 || !point.every(Number.isFinite)) continue
    // Keep the minimap within 20 metres of the robot, excluding corrupt distant map patches.
    if (Math.hypot(point[0] - snapshot.robot.x, point[1], point[2] + snapshot.robot.y) > 20) continue
    positions.push(...point)
    colors.push(...snapshot.colors.slice(i, i + 3))
  }
  return { ...snapshot, positions: new Float32Array(positions), colors: new Uint8Array(colors) }
}
