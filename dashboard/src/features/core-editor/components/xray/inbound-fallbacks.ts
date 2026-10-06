import type { Fallback } from '@pasarguard/xray-config-kit'

export type FallbackEditorRow = {
  id: string
  name: string
  alpn: string
  path: string
  dest: string
  xver: 0 | 1 | 2
}

export function newFallbackEditorRow(): FallbackEditorRow {
  return {
    id: globalThis.crypto?.randomUUID?.() ?? `r-${Math.random().toString(36).slice(2)}`,
    name: '',
    alpn: '',
    path: '',
    dest: '',
    xver: 0,
  }
}

export function fallbackPathIsInvalid(path: string): boolean {
  const trimmed = path.trim()
  return trimmed !== '' && !trimmed.startsWith('/')
}

export function fallbacksToEditorRows(fallbacks: readonly Fallback[] | undefined): FallbackEditorRow[] {
  if (!fallbacks?.length) return [newFallbackEditorRow()]
  return fallbacks.map((fb, i) => ({
    id: `fb-${i}-${String(fb.dest)}`,
    name: fb.name ?? '',
    alpn: fb.alpn ?? '',
    path: fb.path ?? '',
    dest: typeof fb.dest === 'number' ? String(fb.dest) : String(fb.dest ?? ''),
    xver: fb.xver === 1 || fb.xver === 2 ? fb.xver : 0,
  }))
}

export function editorRowsToFallbacks(rows: FallbackEditorRow[]): Fallback[] | undefined {
  const out: Fallback[] = []
  for (const r of rows) {
    const d = r.dest.trim()
    if (d === '') continue
    const dest: string | number = /^\d+$/.test(d) ? Number(d) : d
    out.push({
      dest,
      ...(r.name.trim() ? { name: r.name.trim() } : {}),
      ...(r.alpn.trim() ? { alpn: r.alpn.trim() } : {}),
      ...(r.path.trim() && !fallbackPathIsInvalid(r.path) ? { path: r.path.trim() } : {}),
      ...(r.xver === 1 || r.xver === 2 ? { xver: r.xver } : {}),
    })
  }
  return out.length > 0 ? out : undefined
}
