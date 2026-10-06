import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { cn } from '@/lib/utils'
import type { Profile } from '@pasarguard/xray-config-kit'
import { useTranslation } from 'react-i18next'
import { useCoreEditorStore } from '@/features/core-editor/state/core-editor-store'
import { filterValidationListBlockingErrors, type ValidationListItem } from '@/features/core-editor/components/shared/validation-list'

export type { ValidationListItem } from '@/features/core-editor/components/shared/validation-list'

function formatXrayProfilePath(path: string, profile: Profile | null | undefined): string {
  if (!profile || !path.startsWith('/')) return path
  const parts = path.split('/')
  if (parts.length < 3) return path

  const collection = parts[1]
  if (collection === 'routing' && parts[2] === 'rules') {
    const ruleIndex = Number(parts[3])
    if (!Number.isInteger(ruleIndex) || ruleIndex < 1) return path
    const rule = profile.routing?.rules?.[ruleIndex - 1] as Record<string, unknown> | undefined
    const label = String(rule?.tag ?? rule?.outboundTag ?? rule?.balancerTag ?? '').trim() || `#${ruleIndex}`
    return ['/', collection, parts[2], label, ...parts.slice(4)].join('/').replace('//', '/')
  }

  const rawIndex = Number(parts[2])
  if (!Number.isInteger(rawIndex) || rawIndex < 1) return path
  const index = rawIndex - 1

  let label = ''
  if (collection === 'inbounds') label = String(profile.inbounds?.[index]?.tag ?? '').trim()
  else if (collection === 'outbounds') label = String(profile.outbounds?.[index]?.tag ?? '').trim()

  if (!label) label = `#${rawIndex}`
  return ['/', collection, label, ...parts.slice(3)].join('/').replace('//', '/')
}

interface ValidationSummaryProps {
  items: ValidationListItem[]
  className?: string
}

const DISPLAY_LIMIT = 48

/** Lists blocking issues from the Xray config kit (strict compile), core-kit, WireGuard, etc. */
export function ValidationSummary({ items, className }: ValidationSummaryProps) {
  const { t } = useTranslation()
  const profile = useCoreEditorStore(s => s.xrayProfile)
  if (items.length === 0) return null
  const errors = filterValidationListBlockingErrors(items)
  const list = errors.length > 0 ? errors : items
  const tone = errors.length > 0 ? 'destructive' : 'default'
  const displayed = list.slice(0, DISPLAY_LIMIT)
  const rest = list.length - displayed.length

  return (
    <Alert variant={tone === 'destructive' ? 'destructive' : 'default'} className={cn(className)}>
      <AlertTitle>{errors.length > 0 ? t('coreEditor.validationErrors', { defaultValue: 'Validation errors' }) : t('coreEditor.validationWarnings', { defaultValue: 'Warnings' })}</AlertTitle>
      <AlertDescription className="space-y-2">
        <ul className="mt-1 list-inside list-disc space-y-1 text-sm">
          {displayed.map((row, idx) => (
            <li key={idx}>
              {row.source === 'core-kit' && (
                <>
                  {formatXrayProfilePath(row.issue.path, profile)}: {row.issue.message}
                </>
              )}
              {row.source === 'xray' && (
                <>
                  {formatXrayProfilePath(row.issue.path, profile)}: {row.issue.message}
                  {row.issue.code ? <span className="ml-1 text-[0.8em] opacity-80">[{row.issue.code}]</span> : null}
                  {row.issue.suggestion ? <span className="block pl-4 text-xs opacity-90">→ {row.issue.suggestion}</span> : null}
                </>
              )}
              {row.source === 'wireguard' && (
                <>
                  {row.issue.path}: {row.issue.message}
                </>
              )}
            </li>
          ))}
        </ul>
        {rest > 0 ? <p className="text-xs opacity-90">{t('coreEditor.validationMore', { count: rest, defaultValue: `…and ${rest} more` })}</p> : null}
      </AlertDescription>
    </Alert>
  )
}
