import { useEffect, useMemo } from 'react'
import { useTranslation } from 'react-i18next'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { useAdminMasters } from '@/features/admins/hooks/use-admin-masters'

interface AdminMasterTabsProps {
  /** Selected master id; undefined = all admins, 0 = admins with no master. */
  value: number | undefined
  onChange: (masterId: number | undefined) => void
  /** Changes whenever the admins list changed, so the counts on the tabs follow. */
  refreshKey?: unknown
}

const ALL = 'all'

/** One tab per master (reseller) above the admins list. Hidden when there is nothing to split. */
export default function AdminMasterTabs({ value, onChange, refreshKey }: AdminMasterTabsProps) {
  const { t } = useTranslation()
  const { data, refetch } = useAdminMasters()
  const masters = useMemo(() => data?.masters ?? [], [data])

  useEffect(() => {
    refetch()
  }, [refreshKey, refetch])

  // A selected master that no longer has admins would leave an empty, unselectable view.
  useEffect(() => {
    if (value !== undefined && data && !masters.some(master => master.id === value)) onChange(undefined)
  }, [value, data, masters, onChange])

  if (masters.length < 2) return null

  return (
    <Tabs value={value === undefined ? ALL : String(value)} onValueChange={next => onChange(next === ALL ? undefined : Number(next))} className="mb-3 w-full">
      <div className="overflow-x-auto pb-1">
        <TabsList className="inline-flex h-auto w-max gap-1">
          <TabsTrigger value={ALL} className="gap-2">
            {t('admins.masters.all', { defaultValue: 'All' })}
            <span className="text-muted-foreground text-xs tabular-nums">{data?.total ?? 0}</span>
          </TabsTrigger>
          {masters.map(master => (
            <TabsTrigger key={master.id} value={String(master.id)} className="gap-2">
              <span dir="ltr">{master.id === 0 ? t('admins.masters.none', { defaultValue: 'No master' }) : master.username}</span>
              <span className="text-muted-foreground text-xs tabular-nums">{master.admins}</span>
            </TabsTrigger>
          ))}
        </TabsList>
      </div>
    </Tabs>
  )
}
