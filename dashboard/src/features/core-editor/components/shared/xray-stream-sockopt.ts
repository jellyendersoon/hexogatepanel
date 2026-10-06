export type SockoptJson = Record<string, unknown>

export function pruneHappyEyeballs(h: Record<string, unknown>): Record<string, unknown> | undefined {
  const out: Record<string, unknown> = {}
  if (typeof h.tryDelayMs === 'number' && Number.isFinite(h.tryDelayMs)) out.tryDelayMs = h.tryDelayMs
  if (typeof h.prioritizeIPv6 === 'boolean') out.prioritizeIPv6 = h.prioritizeIPv6
  if (typeof h.interleave === 'number' && Number.isFinite(h.interleave)) out.interleave = h.interleave
  if (typeof h.maxConcurrentTry === 'number' && Number.isFinite(h.maxConcurrentTry)) out.maxConcurrentTry = h.maxConcurrentTry
  return Object.keys(out).length > 0 ? out : undefined
}

/** Drop empty / unset sockopt keys before persisting. */
export function pruneSockoptObject(raw: SockoptJson | undefined): SockoptJson | undefined {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return undefined
  const out: SockoptJson = {}
  for (const [k, v] of Object.entries(raw)) {
    if (v === undefined || v === null) continue
    if (v === '') continue
    if (k === 'happyEyeballs' && typeof v === 'object' && v !== null && !Array.isArray(v)) {
      const pr = pruneHappyEyeballs(v as Record<string, unknown>)
      if (pr) out[k] = pr
      continue
    }
    if (k === 'customSockopt' && Array.isArray(v)) {
      if (v.length > 0) out[k] = v
      continue
    }
    if (typeof v === 'number' && !Number.isFinite(v)) continue
    out[k] = v
  }
  return Object.keys(out).length > 0 ? out : undefined
}
