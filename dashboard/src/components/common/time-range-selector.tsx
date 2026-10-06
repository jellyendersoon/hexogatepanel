'use client'

import * as React from 'react'
import { addDays } from 'date-fns'
import { DateRange } from 'react-day-picker'
import { cn } from '@/lib/utils'
import { useLatest } from '@/hooks/use-latest'
import { DatePicker } from './date-picker'

interface TimeRangeSelectorProps extends React.HTMLAttributes<HTMLDivElement> {
  onRangeChange: (range: DateRange | undefined) => void
  initialRange?: DateRange
}

export function TimeRangeSelector({ className, onRangeChange, initialRange }: TimeRangeSelectorProps) {
  const [range, setRange] = React.useState<DateRange | undefined>(
    initialRange ?? {
      from: addDays(new Date(), -7), // Default to last 7 days
      to: new Date(),
    },
  )

  const onRangeChangeRef = useLatest(onRangeChange)
  // `range` is initialised from `initialRange` (or the default), so the mount value covers both cases
  const mountRangeRef = React.useRef(range)

  React.useEffect(() => {
    // Propagate initial/default range up on mount
    onRangeChangeRef.current(mountRangeRef.current)
  }, [onRangeChangeRef]) // Run only on mount

  const handleRangeChange = (newRange: DateRange | undefined) => {
    setRange(newRange)
    onRangeChange(newRange)
  }

  return (
    <div className={cn(className)}>
      <DatePicker mode="range" range={range} onRangeChange={handleRangeChange} defaultRange={range} disableAfter={new Date()} numberOfMonths={2} />
    </div>
  )
}
