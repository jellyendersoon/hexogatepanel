export type TimeUnit = 'seconds' | 'minutes' | 'hours' | 'days' | 'months'

export const TIME_UNIT_SECONDS: Record<TimeUnit, number> = {
  seconds: 1,
  minutes: 60,
  hours: 60 * 60,
  days: 24 * 60 * 60,
  months: 30 * 24 * 60 * 60,
}

export const secondsToTimeUnit = (seconds: unknown, unit: TimeUnit) => {
  const value = Number(seconds)
  if (!Number.isFinite(value) || value <= 0) return ''
  return String(value / TIME_UNIT_SECONDS[unit])
}
