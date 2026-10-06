import type { CoreKitValidationIssue } from '@pasarguard/core-kit'
import type { Issue } from '@pasarguard/xray-config-kit'
import type { WireGuardValidationIssue } from '@pasarguard/wireguard-config-kit'

export type ValidationListItem = { source: 'core-kit'; issue: CoreKitValidationIssue } | { source: 'xray'; issue: Issue } | { source: 'wireguard'; issue: WireGuardValidationIssue }

export function validationListItemPath(item: ValidationListItem): string {
  const p = item.issue.path
  return typeof p === 'string' ? p : ''
}

/** Same semantics as the “Validation errors” list (not warnings / info-only). */
export function filterValidationListBlockingErrors(items: ValidationListItem[]): ValidationListItem[] {
  return items.filter(i => {
    if (i.source === 'core-kit') return i.issue.severity !== 'warning' && i.issue.severity !== 'info'
    if (i.source === 'xray') return i.issue.severity !== 'warning' && i.issue.severity !== 'info'
    return true
  })
}

export function formatValidationListItemLine(row: ValidationListItem): string {
  if (row.source === 'core-kit') return `${row.issue.path}: ${row.issue.message}`
  if (row.source === 'xray') {
    const code = row.issue.code ? ` [${row.issue.code}]` : ''
    return `${row.issue.path}: ${row.issue.message}${code}`
  }
  return `${row.issue.path}: ${row.issue.message}`
}

export function formatValidationListItemsToastLines(items: ValidationListItem[], limit = 8): string {
  return items.slice(0, limit).map(formatValidationListItemLine).join('\n')
}

/** Keep only issues under a JSON-pointer prefix (e.g. `/inbounds/3` for the third inbound; kit paths are 1-based). */
export function filterValidationListItemsByPathPrefix(items: ValidationListItem[], prefix: string | undefined): ValidationListItem[] {
  if (prefix == null || prefix.trim() === '') return items
  const base = prefix.replace(/\/$/, '')
  return items.filter(item => {
    const p = validationListItemPath(item)
    if (p === '') return false
    return p === base || p.startsWith(`${base}/`)
  })
}
