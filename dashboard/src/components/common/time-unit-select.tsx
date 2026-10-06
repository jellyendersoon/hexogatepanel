import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { useTranslation } from 'react-i18next'

import type { TimeUnit } from '@/components/common/time-unit'

export type { TimeUnit } from '@/components/common/time-unit'

const capitalize = (value: string) => (value ? value.charAt(0).toLocaleUpperCase() + value.slice(1) : value)

interface TimeUnitSelectProps {
  value: TimeUnit
  onValueChange: (value: TimeUnit) => void
  triggerClassName?: string
}

export function TimeUnitSelect({ value, onValueChange, triggerClassName }: TimeUnitSelectProps) {
  const { t } = useTranslation()
  const labels: Record<TimeUnit, string> = {
    seconds: capitalize(t('time.seconds', { defaultValue: 'Seconds' })),
    minutes: capitalize(t('time.mins', { defaultValue: 'Minutes' })),
    hours: capitalize(t('time.hours', { defaultValue: 'Hours' })),
    days: capitalize(t('time.days', { defaultValue: 'Days' })),
    months: capitalize(t('time.months', { defaultValue: 'Months' })),
  }

  return (
    <Select value={value} onValueChange={v => onValueChange(v as TimeUnit)}>
      <SelectTrigger className={triggerClassName}>
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        <SelectItem value="seconds">{labels.seconds}</SelectItem>
        <SelectItem value="minutes">{labels.minutes}</SelectItem>
        <SelectItem value="hours">{labels.hours}</SelectItem>
        <SelectItem value="days">{labels.days}</SelectItem>
        <SelectItem value="months">{labels.months}</SelectItem>
      </SelectContent>
    </Select>
  )
}
