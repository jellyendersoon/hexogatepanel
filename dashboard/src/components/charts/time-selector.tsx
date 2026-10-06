import { useMemo, useState } from 'react'
import { Button } from '@/components/ui/button'
import { DropdownMenu, DropdownMenuContent, DropdownMenuRadioGroup, DropdownMenuRadioItem, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { cn } from '@/lib/utils'
import { useTranslation } from 'react-i18next'

import { DEFAULT_TIME_SELECTOR_SHORTCUTS, type TimeSelectorShortcut } from '@/components/charts/time-selector-shortcuts'

export type { TimePeriod, TimeSelectorShortcut } from '@/components/charts/time-selector-shortcuts'

interface TimeSelectorProps {
  selectedTime: string
  setSelectedTime: (value: string) => void
  shortcuts?: readonly TimeSelectorShortcut[]
  maxVisible?: number
  className?: string
}

export default function TimeSelector({ selectedTime, setSelectedTime, shortcuts = DEFAULT_TIME_SELECTOR_SHORTCUTS, maxVisible = shortcuts.length, className }: TimeSelectorProps) {
  const { t } = useTranslation()
  const [isMobileMoreOpen, setIsMobileMoreOpen] = useState(false)
  const [isDesktopMoreOpen, setIsDesktopMoreOpen] = useState(false)
  const moreLabelRaw = t('more', { defaultValue: 'More' })
  const moreLabel = moreLabelRaw ? moreLabelRaw.charAt(0).toLocaleUpperCase() + moreLabelRaw.slice(1) : 'More'

  const getShortcutLabel = (shortcut: TimeSelectorShortcut) => {
    if (shortcut.value === 'all') {
      return t('alltime', { defaultValue: 'All Time' })
    }
    return shortcut.label
  }

  const quickShortcuts = useMemo(() => {
    const explicitQuick = shortcuts.filter(shortcut => shortcut.quick)
    if (explicitQuick.length > 0) return explicitQuick
    return shortcuts.slice(0, maxVisible)
  }, [shortcuts, maxVisible])

  const desktopOverflowShortcuts = useMemo(() => {
    const quickValues = new Set(quickShortcuts.map(shortcut => shortcut.value))
    return shortcuts.filter(shortcut => !quickValues.has(shortcut.value))
  }, [shortcuts, quickShortcuts])

  const mobileQuickShortcuts = useMemo(() => quickShortcuts.slice(0, 4), [quickShortcuts])

  const mobileOverflowShortcuts = useMemo(() => {
    const quickValues = new Set(mobileQuickShortcuts.map(shortcut => shortcut.value))
    return shortcuts.filter(shortcut => !quickValues.has(shortcut.value))
  }, [shortcuts, mobileQuickShortcuts])

  const isMobileOverflowSelected = mobileOverflowShortcuts.some(shortcut => shortcut.value === selectedTime)
  const isDesktopOverflowSelected = desktopOverflowShortcuts.some(shortcut => shortcut.value === selectedTime)

  return (
    <div dir="ltr" className={cn('border-border/60 bg-muted/20 flex h-9 w-full max-w-full min-w-0 items-center overflow-hidden rounded-md border p-0.5 sm:max-w-fit', className)}>
      <div className="flex h-full w-full min-w-0 items-center gap-1 lg:hidden">
        <ToggleGroup
          type="single"
          value={selectedTime}
          onValueChange={value => value && setSelectedTime(value)}
          className="h-full min-w-0 [scrollbar-width:none] flex-nowrap gap-1 overflow-x-auto [&::-webkit-scrollbar]:hidden"
          aria-label="Traffic range shortcuts"
        >
          {mobileQuickShortcuts.map(shortcut => (
            <ToggleGroupItem
              key={shortcut.value}
              value={shortcut.value}
              variant="default"
              className="text-muted-foreground data-[state=on]:bg-background data-[state=on]:text-foreground h-8 min-w-[2.25rem] shrink-0 border-0 bg-transparent px-2.5 py-0 text-xs font-medium data-[state=on]:shadow-sm"
            >
              {getShortcutLabel(shortcut)}
            </ToggleGroupItem>
          ))}
        </ToggleGroup>
        {mobileOverflowShortcuts.length > 0 && (
          <DropdownMenu modal={false} open={isMobileMoreOpen} onOpenChange={setIsMobileMoreOpen}>
            <DropdownMenuTrigger asChild>
              <Button
                variant="ghost"
                size="sm"
                className={cn(
                  'text-muted-foreground hover:bg-background/70 hover:text-foreground h-8 min-w-[3.75rem] border-0 bg-transparent px-2 py-0 text-xs font-medium shadow-none',
                  isMobileOverflowSelected && 'bg-background text-foreground shadow-sm',
                )}
              >
                {moreLabel}
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="min-w-[7rem]" onInteractOutside={() => setIsMobileMoreOpen(false)}>
              <DropdownMenuRadioGroup
                value={selectedTime}
                onValueChange={value => {
                  setSelectedTime(value)
                  setIsMobileMoreOpen(false)
                }}
              >
                {mobileOverflowShortcuts.map(shortcut => (
                  <DropdownMenuRadioItem key={shortcut.value} value={shortcut.value} className="text-xs">
                    {getShortcutLabel(shortcut)}
                  </DropdownMenuRadioItem>
                ))}
              </DropdownMenuRadioGroup>
            </DropdownMenuContent>
          </DropdownMenu>
        )}
      </div>
      <div className="hidden h-full w-full min-w-0 items-center gap-1 lg:flex">
        <ToggleGroup
          type="single"
          value={selectedTime}
          onValueChange={value => value && setSelectedTime(value)}
          className="h-full min-w-0 [scrollbar-width:none] flex-nowrap gap-1 overflow-x-auto [&::-webkit-scrollbar]:hidden"
          aria-label="Traffic range shortcuts"
        >
          {quickShortcuts.map(shortcut => (
            <ToggleGroupItem
              key={shortcut.value}
              value={shortcut.value}
              variant="default"
              className="text-muted-foreground data-[state=on]:bg-background data-[state=on]:text-foreground h-8 min-w-[2.25rem] shrink-0 border-0 bg-transparent px-2.5 py-0 text-xs font-medium data-[state=on]:shadow-sm"
            >
              {getShortcutLabel(shortcut)}
            </ToggleGroupItem>
          ))}
        </ToggleGroup>
        {desktopOverflowShortcuts.length > 0 && (
          <DropdownMenu modal={false} open={isDesktopMoreOpen} onOpenChange={setIsDesktopMoreOpen}>
            <DropdownMenuTrigger asChild>
              <Button
                variant="ghost"
                size="sm"
                className={cn(
                  'text-muted-foreground hover:bg-background/70 hover:text-foreground h-8 min-w-[3.75rem] border-0 bg-transparent px-2 py-0 text-xs font-medium shadow-none',
                  isDesktopOverflowSelected && 'bg-background text-foreground shadow-sm',
                )}
              >
                {moreLabel}
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="min-w-[7rem]" onInteractOutside={() => setIsDesktopMoreOpen(false)}>
              <DropdownMenuRadioGroup
                value={selectedTime}
                onValueChange={value => {
                  setSelectedTime(value)
                  setIsDesktopMoreOpen(false)
                }}
              >
                {desktopOverflowShortcuts.map(shortcut => (
                  <DropdownMenuRadioItem key={shortcut.value} value={shortcut.value} className="text-xs">
                    {getShortcutLabel(shortcut)}
                  </DropdownMenuRadioItem>
                ))}
              </DropdownMenuRadioGroup>
            </DropdownMenuContent>
          </DropdownMenu>
        )}
      </div>
    </div>
  )
}
