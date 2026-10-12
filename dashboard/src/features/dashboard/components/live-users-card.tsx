import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { cn } from '@/lib/utils'
import useDirDetection from '@/hooks/use-dir-detection'
import { fetch } from '@/service/http'
import { numberWithCommas } from '@/utils/formatByte'
import { useQuery } from '@tanstack/react-query'
import { Users } from 'lucide-react'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { formatBitRate, Sparkline } from './live-fleet-card'

type LiveUser = {
  id: number
  username: string
  admin: string | null
  status: string | null
  used_traffic: number | null
  data_limit: number | null
  bps: number
  bytes: number
  kept_bytes: number
  servers: { name: string; bytes: number }[]
  spark: number[]
}

type LiveUsers = {
  age: number | null
  seconds: number
  active: number
  total_bps: number
  span: 'now' | '10m'
  users: LiveUser[]
}

const SPANS = [
  { value: 'now', label: 'Now' },
  { value: '10m', label: '10 min' },
] as const

const formatSize = (bytes: number | null | undefined): string => {
  if (bytes === null || bytes === undefined) return '--'
  if (bytes >= 1024 ** 4) return `${(bytes / 1024 ** 4).toFixed(2)} TB`
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(bytes >= 10 * 1024 ** 3 ? 0 : 1)} GB`
  if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(0)} MB`
  return `${Math.max(0, Math.round(bytes / 1024))} KB`
}

const LiveUsersCard = () => {
  const { t } = useTranslation()
  const dir = useDirDetection()
  const [span, setSpan] = useState<'now' | '10m'>('now')

  const { data, isLoading, isError } = useQuery({
    queryKey: ['fleetLiveUsers', span],
    queryFn: () => fetch<LiveUsers>('/api/fleet/live/users', { query: { limit: 10, span } }),
    refetchInterval: 10000,
    retry: false,
  })

  const users = data?.users ?? []
  const top = users[0]?.bps || 1

  return (
    <Card className="bg-card/80 border" dir={dir}>
      <CardHeader className="p-2">
        <div className="flex w-full flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex min-w-0 items-center gap-2 p-2">
            <div className="bg-primary/10 rounded-md p-1.5">
              <Users className="text-primary h-4 w-4" />
            </div>
            <div className="min-w-0">
              <CardTitle className="truncate text-sm font-semibold">{t('fleetLive.usersTitle', { defaultValue: 'Top users live' })}</CardTitle>
              <p className="text-muted-foreground truncate text-xs">{t('fleetLive.usersSubtitle', { defaultValue: 'The ten users moving the most data, updated every 30 seconds' })}</p>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2 px-2 text-xs">
            {data && data.age !== null && (
              <Badge variant="green">
                {t('fleetLive.usersActive', { defaultValue: '{{count}} users active · {{speed}}', count: numberWithCommas(data.active) ?? data.active, speed: formatBitRate(data.total_bps) })}
              </Badge>
            )}
            <div className="bg-muted/40 flex items-center rounded-md border p-0.5">
              {SPANS.map(option => (
                <button
                  key={option.value}
                  type="button"
                  onClick={() => setSpan(option.value)}
                  className={cn('rounded px-2 py-0.5 transition-colors', span === option.value ? 'bg-background text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground')}
                >
                  {t(`fleetLive.span.${option.value}`, { defaultValue: option.label })}
                </button>
              ))}
            </div>
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-3 px-4">
        {isLoading ? (
          <Skeleton className="h-40 w-full" />
        ) : isError || !data ? (
          <div className="border-destructive/30 bg-destructive/10 text-destructive rounded-md border px-3 py-2 text-xs">{t('fleetLive.usersError', { defaultValue: 'Unable to load live user data.' })}</div>
        ) : users.length === 0 ? (
          <p className="text-muted-foreground py-6 text-center text-xs">{t('fleetLive.usersEmpty', { defaultValue: 'Waiting for the next usage report from the servers.' })}</p>
        ) : (
          <>
            <div className="overflow-x-auto rounded-md border">
              <table className="w-full min-w-[720px] text-xs">
                <thead className="bg-muted/40 text-muted-foreground">
                  <tr>
                    <th className="w-8 px-2 py-1.5 text-start font-medium">#</th>
                    <th className="px-2 py-1.5 text-start font-medium">{t('fleetLive.user', { defaultValue: 'User' })}</th>
                    <th className="px-2 py-1.5 text-start font-medium">{t('fleetLive.owner', { defaultValue: 'Admin' })}</th>
                    <th className="px-2 py-1.5 text-start font-medium">{t('fleetLive.speed', { defaultValue: 'Speed' })}</th>
                    <th className="px-2 py-1.5 text-end font-medium">{t('fleetLive.last10', { defaultValue: 'Last 10 min' })}</th>
                    <th className="px-2 py-1.5 text-end font-medium">{t('fleetLive.usedTotal', { defaultValue: 'Used / limit' })}</th>
                    <th className="px-2 py-1.5 text-start font-medium">{t('fleetLive.servers', { defaultValue: 'Servers' })}</th>
                    <th className="px-2 py-1.5 text-start font-medium">{t('fleetLive.trend', { defaultValue: 'Trend' })}</th>
                  </tr>
                </thead>
                <tbody>
                  {users.map((user, index) => (
                    <tr key={user.id} className="hover:bg-muted/30 border-t transition-colors">
                      <td className="text-muted-foreground px-2 py-1.5 tabular-nums">{index + 1}</td>
                      <td className="max-w-[180px] px-2 py-1.5">
                        <span className="block truncate font-medium" dir="ltr" title={user.username}>
                          {user.username}
                        </span>
                      </td>
                      <td className="text-muted-foreground max-w-[120px] truncate px-2 py-1.5" dir="ltr">
                        {user.admin ?? '--'}
                      </td>
                      <td className="px-2 py-1.5">
                        <div className="flex items-center gap-2">
                          <div className="bg-muted h-1.5 w-16 overflow-hidden rounded-full">
                            <div className="bg-primary/80 h-full rounded-full" style={{ width: `${Math.max(2, Math.min(100, (user.bps / top) * 100))}%` }} />
                          </div>
                          <span className="font-medium whitespace-nowrap tabular-nums" dir="ltr">
                            {formatBitRate(user.bps)}
                          </span>
                        </div>
                      </td>
                      <td className="px-2 py-1.5 text-end whitespace-nowrap tabular-nums" dir="ltr">
                        {formatSize(user.kept_bytes)}
                      </td>
                      <td className="text-muted-foreground px-2 py-1.5 text-end whitespace-nowrap tabular-nums" dir="ltr">
                        {formatSize(user.used_traffic)} / {user.data_limit ? formatSize(user.data_limit) : '∞'}
                      </td>
                      <td className="max-w-[220px] truncate px-2 py-1.5" title={user.servers.map(server => `${server.name} ${formatSize(server.bytes)}`).join(' · ')}>
                        {user.servers.map(server => server.name).join(' · ')}
                      </td>
                      <td className="text-primary px-2 py-1.5">
                        <Sparkline values={user.spark} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="text-muted-foreground text-[11px]">
              {t('fleetLive.usersNote', {
                defaultValue: 'Speed is download plus upload, averaged over the last usage report (about 30 seconds) or the last ten minutes. One account shared by many devices shows as one user.',
              })}
            </p>
          </>
        )}
      </CardContent>
    </Card>
  )
}

export default LiveUsersCard
