/**
 * Client-side dendrogram cut (scipy linkage-matrix format: rows [a, b, distance, count], n observations,
 * merged cluster i gets id n + i). Same rule as scipy fcluster(criterion="distance"): observations whose
 * cophenetic distance is ≤ h share a cluster.
 */

export function cutLinkage(linkage: number[][], h: number): number[] {
  const n = linkage.length + 1
  const parent = Array.from({ length: 2 * n - 1 }, (_, i) => i)
  const find = (i: number): number => {
    while (parent[i] !== i) {
      parent[i] = parent[parent[i]]
      i = parent[i]
    }
    return i
  }
  linkage.forEach((row, i) => {
    if (row[2] <= h) {
      parent[find(row[0])] = n + i
      parent[find(row[1])] = n + i
    }
  })
  // relabel roots 0..k-1 in order of first appearance
  const ids = new Map<number, number>()
  return Array.from({ length: n }, (_, i) => {
    const r = find(i)
    if (!ids.has(r)) ids.set(r, ids.size)
    return ids.get(r) as number
  })
}

export const clusterCount = (labels: number[]): number => new Set(labels).size

/** Cut height that yields k clusters (midpoint between merge heights), or null if k is out of range. */
export function heightForK(linkage: number[][], k: number): number | null {
  const n = linkage.length + 1
  if (k < 1 || k > n) return null
  const hs = linkage.map((r) => r[2]).sort((a, b) => a - b)
  if (k === n) return hs[0] / 2
  if (k === 1) return hs[hs.length - 1] * 1.02
  const lo = hs[n - k - 1]
  const hi = hs[n - k]
  return (lo + hi) / 2
}

/**
 * Cluster index of each dendrogram link (U shape) at cut h, or -1 when the link sits above the cut.
 * Leaf at position p is drawn at x = 10p + 5; a link below the cut lies entirely inside one cluster,
 * so the leaf nearest its left leg identifies it.
 */
export function linkClusters(icoord: number[][], dcoord: number[][], leaves: number[], labels: number[], h: number): number[] {
  return icoord.map((xs, i) => {
    if (dcoord[i][1] > h) return -1
    const pos = Math.min(leaves.length - 1, Math.max(0, Math.round((xs[0] - 5) / 10)))
    const obs = leaves[pos]
    return obs !== undefined && obs < labels.length ? labels[obs] : -1
  })
}
