import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { ChartConfig, ChartContainer, ChartTooltip } from '@/components/ui/chart'
import { Skeleton } from '@/components/ui/skeleton'
import { cn } from '@/lib/utils'
import useDirDetection from '@/hooks/use-dir-detection'
import { fetch } from '@/service/http'
import { numberWithCommas } from '@/utils/formatByte'
import { useQuery } from '@tanstack/react-query'
import { Activity, ArrowDown, ArrowDownUp, ArrowUp, ChevronDown, ChevronRight, Network } from 'lucide-react'
import { Fragment, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Area, AreaChart, CartesianGrid, XAxis, YAxis } from 'recharts'

type FleetInbound = {
  port: number
  connections: number
  tag: string
  protocol: string
  network: string
  security?: string
}

type FleetServer = {
  key: string
  name: string
  nodes: { id: number; name: string; status: string }[]
  status: 'live' | 'stale' | 'offline' | 'no_reporter'
  age: number | null
  rx_bps: number | null
  tx_bps: number | null
  tcp: number | null
  udp: number | null
  cpu: number | null
  steal: number | null
  cores: number | null
  mem_used: number | null
  mem_total: number | null
  load1: number | null
  spark: number[]
  inbounds: FleetInbound[]
}

type FleetLive = {
  generated: number
  totals: { rx_bps: number; tx_bps: number; tcp: number; udp: number; servers: number; reporting: number }
  servers: FleetServer[]
}

type FleetHistory = { points: [number, number, number, number, number][] }

type SortKey = 'speed' | 'rx_bps' | 'tx_bps' | 'tcp' | 'udp' | 'cpu' | 'name'

const RANGES = [
  { minutes: 30, label: '30m' },
  { minutes: 360, label: '6h' },
  { minutes: 1440, label: '24h' },
] as const

const chartConfig = {
  rx: { label: 'In', color: 'hsl(var(--chart-1))' },
  tx: { label: 'Out', color: 'hsl(var(--chart-2))' },
} satisfies ChartConfig

export const formatBitRate = (bits: number | null | undefined): string => {
  if (bits === null || bits === undefined || Number.isNaN(bits)) return '--'
  if (bits >= 1e9) return `${(bits / 1e9).toFixed(2)} Gbit/s`
  if (bits >= 1e6) return `${(bits / 1e6).toFixed(bits >= 1e8 ? 0 : 1)} Mbit/s`
  if (bits >= 1e3) return `${(bits / 1e3).toFixed(0)} kbit/s`
  return `${Math.round(bits)} bit/s`
}

const formatCount = (value: number | null | undefined) => (value === null || value === undefined ? '--' : (numberWithCommas(value) ?? '--'))

const statusDot: Record<FleetServer['status'], string> = {
  live: 'bg-emerald-500',
  stale: 'bg-amber-500',
  offline: 'bg-rose-500',
  no_reporter: 'bg-muted-foreground/40',
}

/** A server needs attention when its CPU is nearly exhausted or the hypervisor is taking a large share of it. */
const isStrained = (server: FleetServer) => (server.cpu ?? 0) >= 90 || (server.steal ?? 0) >= 35

export const Sparkline = ({ values, className }: { values: number[]; className?: string }) => {
  const path = useMemo(() => {
    const max = Math.max(1, ...values)
    const step = values.length > 1 ? 100 / (values.length - 1) : 100
    let line = ''
    let started = false
    values.forEach((value, index) => {
      if (value < 0) {
        started = false
        return
      }
      const x = (index * step).toFixed(2)
      const y = (24 - (value / max) * 22 - 1).toFixed(2)
      line += `${started ? 'L' : 'M'}${x},${y}`
      started = true
    })
    return line
  }, [values])
  if (!path) return <div className={cn('h-6 w-24', className)} />
  return (
    <svg viewBox="0 0 100 24" preserveAspectRatio="none" className={cn('h-6 w-24', className)} aria-hidden="true">
      <path d={path} fill="none" stroke="currentColor" strokeWidth="1.5" vectorEffect="non-scaling-stroke" strokeLinejoin="round" />
    </svg>
  )
}

const Meter = ({ value, steal }: { value: number | null; steal: number | null }) => {
  if (value === null) return <span className="text-muted-foreground">--</span>
  const busy = Math.min(100, Math.max(0, value))
  const stolen = Math.min(busy, Math.max(0, steal ?? 0))
  return (
    <div className="flex items-center gap-2">
      <div className="bg-muted relative h-1.5 w-14 overflow-hidden rounded-full">
        <div className={cn('absolute inset-y-0 left-0 rounded-full', busy >= 90 ? 'bg-rose-500' : busy >= 75 ? 'bg-amber-500' : 'bg-emerald-500')} style={{ width: `${busy}%` }} />
        {stolen > 0 && <div className="absolute inset-y-0 left-0 rounded-full bg-violet-500/90" style={{ width: `${stolen}%` }} />}
      </div>
      <span className="tabular-nums">{Math.round(busy)}%</span>
    </div>
  )
}

const Tile = ({ icon: Icon, label, value, sub }: { icon: typeof Activity; label: string; value: string; sub?: string }) => (
  <div className="bg-muted/30 flex items-center gap-3 rounded-md border px-3 py-2">
    <div className="bg-primary/10 rounded-md p-1.5">
      <Icon className="text-primary h-4 w-4" />
    </div>
    <div className="min-w-0">
      <p className="text-muted-foreground truncate text-xs">{label}</p>
      <p className="truncate text-base leading-tight font-semibold tabular-nums" dir="ltr">
        {value}
      </p>
      {sub && <p className="text-muted-foreground truncate text-[11px]">{sub}</p>}
    </div>
  </div>
)

const LiveFleetCard = () => {
  const { t } = useTranslation()
  const dir = useDirDetection()
  const [expanded, setExpanded] = useState<string | null>(null)
  const [sortKey, setSortKey] = useState<SortKey>('speed')
  const [range, setRange] = useState<number>(30)
  const [showChart, setShowChart] = useState(true)

  const { data, isLoading, isError } = useQuery({
    queryKey: ['fleetLive'],
    queryFn: () => fetch<FleetLive>('/api/fleet/live'),
    refetchInterval: 4000,
    retry: false,
  })

  const { data: history } = useQuery({
    queryKey: ['fleetLiveHistory', range],
    queryFn: () => fetch<FleetHistory>('/api/fleet/live/history', { query: { minutes: range } }),
    refetchInterval: 30000,
    enabled: showChart,
    retry: false,
  })

  const servers = useMemo(() => {
    const list = [...(data?.servers ?? [])]
    const value = (server: FleetServer): number => {
      if (sortKey === 'speed') return (server.rx_bps ?? 0) + (server.tx_bps ?? 0)
      if (sortKey === 'name') return 0
      return server[sortKey] ?? -1
    }
    if (sortKey === 'name') list.sort((a, b) => a.name.localeCompare(b.name))
    else list.sort((a, b) => value(b) - value(a))
    return list
  }, [data?.servers, sortKey])

  const chartData = useMemo(
    () => (history?.points ?? []).map(([ts, rx, tx, tcp, udp]) => ({ ts: ts * 1000, rx, tx, tcp, udp })),
    [history?.points],
  )

  const totals = data?.totals
  const strained = servers.filter(isStrained).length
  const down = servers.filter(server => server.status === 'offline').length

  const header = (key: SortKey, label: string, align: 'start' | 'end' = 'end') => (
    <th className={cn('px-2 py-1.5 font-medium whitespace-nowrap', align === 'end' ? 'text-end' : 'text-start')}>
      <button type="button" onClick={() => setSortKey(key)} className={cn('hover:text-foreground transition-colors', sortKey === key && 'text-foreground underline underline-offset-4')}>
        {label}
      </button>
    </th>
  )

  const timeTick = (value: number) => new Date(value).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false })

  return (
    <Card className="bg-card/80 border" dir={dir}>
      <CardHeader className="p-2">
        <div className="flex w-full flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex min-w-0 items-center gap-2 p-2">
            <div className="bg-primary/10 rounded-md p-1.5">
              <Activity className="text-primary h-4 w-4" />
            </div>
            <div className="min-w-0">
              <CardTitle className="truncate text-sm font-semibold">{t('fleetLive.title', { defaultValue: 'Live fleet' })}</CardTitle>
              <p className="text-muted-foreground truncate text-xs">{t('fleetLive.subtitle', { defaultValue: 'Traffic and connections on every server, right now' })}</p>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2 px-2 text-xs">
            {totals && (
              <Badge variant={down ? 'red' : strained ? 'yellow' : 'green'}>
                {t('fleetLive.reporting', { defaultValue: '{{reporting}} of {{servers}} reporting', reporting: totals.reporting, servers: totals.servers })}
              </Badge>
            )}
            <div className="bg-muted/40 flex items-center rounded-md border p-0.5">
              {RANGES.map(option => (
                <button
                  key={option.minutes}
                  type="button"
                  onClick={() => {
                    setRange(option.minutes)
                    setShowChart(true)
                  }}
                  className={cn('rounded px-2 py-0.5 tabular-nums transition-colors', showChart && range === option.minutes ? 'bg-background text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground')}
                >
                  {option.label}
                </button>
              ))}
            </div>
            <Button variant="ghost" size="sm" className="h-7 px-2 text-xs" onClick={() => setShowChart(prev => !prev)}>
              {showChart ? t('fleetLive.hideChart', { defaultValue: 'Hide chart' }) : t('fleetLive.showChart', { defaultValue: 'Show chart' })}
            </Button>
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-3 px-4">
        {isLoading ? (
          <div className="space-y-2">
            <Skeleton className="h-14 w-full" />
            <Skeleton className="h-40 w-full" />
          </div>
        ) : isError || !data ? (
          <div className="border-destructive/30 bg-destructive/10 text-destructive rounded-md border px-3 py-2 text-xs">{t('fleetLive.error', { defaultValue: 'Unable to load live fleet data.' })}</div>
        ) : (
          <>
            <div className="grid grid-cols-2 gap-2 lg:grid-cols-4">
              <Tile icon={ArrowDown} label={t('fleetLive.inbound', { defaultValue: 'Inbound speed' })} value={formatBitRate(totals?.rx_bps)} />
              <Tile icon={ArrowUp} label={t('fleetLive.outbound', { defaultValue: 'Outbound speed' })} value={formatBitRate(totals?.tx_bps)} />
              <Tile icon={Network} label={t('fleetLive.tcp', { defaultValue: 'TCP connections' })} value={formatCount(totals?.tcp)} />
              <Tile icon={ArrowDownUp} label={t('fleetLive.udp', { defaultValue: 'UDP flows' })} value={formatCount(totals?.udp)} />
            </div>

            {showChart && (
              <div className="rounded-md border p-2">
                {chartData.length < 2 ? (
                  <p className="text-muted-foreground px-2 py-6 text-center text-xs">{t('fleetLive.noHistory', { defaultValue: 'History appears here after a few minutes of reports.' })}</p>
                ) : (
                  <ChartContainer config={chartConfig} dir="ltr" className="h-[180px] w-full">
                    <AreaChart data={chartData} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
                      <CartesianGrid vertical={false} strokeDasharray="3 3" />
                      <XAxis dataKey="ts" type="number" domain={['dataMin', 'dataMax']} tickFormatter={timeTick} tickLine={false} axisLine={false} minTickGap={48} fontSize={11} />
                      <YAxis tickFormatter={(value: number) => formatBitRate(value)} tickLine={false} axisLine={false} width={78} fontSize={11} />
                      <ChartTooltip
                        cursor={false}
                        content={({ active, payload }) => {
                          if (!active || !payload?.length) return null
                          const point = payload[0].payload as { ts: number; rx: number; tx: number; tcp: number; udp: number }
                          return (
                            <div className="bg-background rounded-md border px-3 py-2 text-xs shadow-md" dir="ltr">
                              <p className="mb-1 font-medium">{new Date(point.ts).toLocaleString([], { hour12: false })}</p>
                              <p>
                                {t('fleetLive.in', { defaultValue: 'In' })}: <span className="font-medium tabular-nums">{formatBitRate(point.rx)}</span>
                              </p>
                              <p>
                                {t('fleetLive.out', { defaultValue: 'Out' })}: <span className="font-medium tabular-nums">{formatBitRate(point.tx)}</span>
                              </p>
                              <p className="text-muted-foreground">
                                TCP {formatCount(point.tcp)} · UDP {formatCount(point.udp)}
                              </p>
                            </div>
                          )
                        }}
                      />
                      <Area dataKey="tx" type="monotone" stroke="var(--color-tx)" fill="var(--color-tx)" fillOpacity={0.15} strokeWidth={1.5} isAnimationActive={false} />
                      <Area dataKey="rx" type="monotone" stroke="var(--color-rx)" fill="var(--color-rx)" fillOpacity={0.15} strokeWidth={1.5} isAnimationActive={false} />
                    </AreaChart>
                  </ChartContainer>
                )}
              </div>
            )}

            <div className="overflow-x-auto rounded-md border">
              <table className="w-full min-w-[720px] text-xs">
                <thead className="bg-muted/40 text-muted-foreground">
                  <tr>
                    {header('name', t('fleetLive.server', { defaultValue: 'Server' }), 'start')}
                    {header('rx_bps', t('fleetLive.in', { defaultValue: 'In' }))}
                    {header('tx_bps', t('fleetLive.out', { defaultValue: 'Out' }))}
                    {header('tcp', 'TCP')}
                    {header('udp', 'UDP')}
                    {header('cpu', 'CPU', 'start')}
                    <th className="px-2 py-1.5 text-start font-medium">{t('fleetLive.memory', { defaultValue: 'Memory' })}</th>
                    {header('speed', t('fleetLive.last30', { defaultValue: 'Last 30 min' }), 'start')}
                  </tr>
                </thead>
                <tbody>
                  {servers.map(server => {
                    const open = expanded === server.key
                    const memory = server.mem_total ? Math.round(((server.mem_used ?? 0) / server.mem_total) * 100) : null
                    const totalConnections = server.inbounds.reduce((sum, inbound) => sum + inbound.connections, 0)
                    return (
                      <Fragment key={server.key}>
                        <tr className={cn('hover:bg-muted/30 cursor-pointer border-t transition-colors', isStrained(server) && 'bg-amber-500/5')} onClick={() => setExpanded(open ? null : server.key)}>
                          <td className="px-2 py-1.5">
                            <div className="flex items-center gap-2">
                              {open ? <ChevronDown className="h-3.5 w-3.5 shrink-0" /> : <ChevronRight className={cn('h-3.5 w-3.5 shrink-0', dir === 'rtl' && 'rotate-180')} />}
                              <span className={cn('h-2 w-2 shrink-0 rounded-full', statusDot[server.status])} />
                              <span className="truncate font-medium">{server.name}</span>
                              {server.status === 'no_reporter' && <span className="text-muted-foreground truncate">{t('fleetLive.noReporter', { defaultValue: 'speed only' })}</span>}
                              {server.status === 'offline' && <span className="truncate text-rose-500">{t('fleetLive.offline', { defaultValue: 'not reporting' })}</span>}
                            </div>
                          </td>
                          <td className="px-2 py-1.5 text-end tabular-nums" dir="ltr">
                            {formatBitRate(server.rx_bps)}
                          </td>
                          <td className="px-2 py-1.5 text-end tabular-nums" dir="ltr">
                            {formatBitRate(server.tx_bps)}
                          </td>
                          <td className="px-2 py-1.5 text-end tabular-nums">{formatCount(server.tcp)}</td>
                          <td className="px-2 py-1.5 text-end tabular-nums">{formatCount(server.udp)}</td>
                          <td className="px-2 py-1.5">
                            <Meter value={server.cpu} steal={server.steal} />
                          </td>
                          <td className="px-2 py-1.5 tabular-nums">{memory === null ? '--' : `${memory}%`}</td>
                          <td className="text-primary px-2 py-1.5">
                            <Sparkline values={server.spark} />
                          </td>
                        </tr>
                        {open && (
                          <tr className="bg-muted/20 border-t">
                            <td colSpan={8} className="px-4 py-3">
                              <div className="text-muted-foreground mb-2 flex flex-wrap gap-x-4 gap-y-1">
                                <span dir="ltr">{server.key}</span>
                                {server.cores !== null && (
                                  <span>
                                    {server.cores} vCPU
                                    {server.steal !== null && server.steal >= 1 ? ` · ${t('fleetLive.steal', { defaultValue: 'steal' })} ${Math.round(server.steal)}%` : ''}
                                    {server.load1 !== null ? ` · ${t('fleetLive.load', { defaultValue: 'load' })} ${server.load1}` : ''}
                                  </span>
                                )}
                                <span>{server.nodes.map(node => node.name).join(' · ')}</span>
                              </div>
                              {server.inbounds.length === 0 ? (
                                <p className="text-muted-foreground">
                                  {server.status === 'no_reporter'
                                    ? t('fleetLive.installHint', { defaultValue: 'Install the live reporter on this server to see connections per inbound.' })
                                    : t('fleetLive.noInbounds', { defaultValue: 'No per-inbound data yet.' })}
                                </p>
                              ) : (
                                <div className="grid gap-1.5 md:grid-cols-2">
                                  {server.inbounds.map(inbound => (
                                    <div key={`${inbound.port}-${inbound.tag}`} className="flex items-center gap-2">
                                      <div className="min-w-0 flex-1">
                                        <div className="flex items-center justify-between gap-2">
                                          <span className="truncate" dir="ltr" title={inbound.tag}>
                                            {inbound.tag}
                                          </span>
                                          <span className="shrink-0 tabular-nums">{formatCount(inbound.connections)}</span>
                                        </div>
                                        <div className="bg-muted mt-0.5 h-1 overflow-hidden rounded-full">
                                          <div className="bg-primary/70 h-full rounded-full" style={{ width: `${totalConnections ? Math.max(1, (inbound.connections / totalConnections) * 100) : 0}%` }} />
                                        </div>
                                      </div>
                                      <span className="text-muted-foreground w-28 shrink-0 truncate text-end" dir="ltr">
                                        {inbound.port ? `:${inbound.port}` : ''} {inbound.network}
                                      </span>
                                    </div>
                                  ))}
                                </div>
                              )}
                            </td>
                          </tr>
                        )}
                      </Fragment>
                    )
                  })}
                  {servers.length === 0 && (
                    <tr>
                      <td colSpan={8} className="text-muted-foreground px-3 py-6 text-center">
                        {t('fleetLive.empty', { defaultValue: 'No servers yet.' })}
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
            <p className="text-muted-foreground text-[11px]">
              {t('fleetLive.note', {
                defaultValue: 'Speed is the whole server, not only customer traffic. UDP has no connections, so the number is open UDP flows. Purple on the CPU bar is time taken by the provider (steal).',
              })}
            </p>
          </>
        )}
      </CardContent>
    </Card>
  )
}

export default LiveFleetCard
