export type TimePeriod = string
export type TimeSelectorShortcut = {
  value: string
  label: string
  quick?: boolean
}

export const DEFAULT_TIME_SELECTOR_SHORTCUTS: TimeSelectorShortcut[] = [
  { value: '24h', label: '24h' },
  { value: '3d', label: '3d' },
  { value: '1w', label: '1w' },
  { value: '1m', label: '1m' },
]

export const TRAFFIC_TIME_SELECTOR_SHORTCUTS: TimeSelectorShortcut[] = [
  { value: '1h', label: '1h', quick: true },
  { value: '2h', label: '2h' },
  { value: '4h', label: '4h' },
  { value: '6h', label: '6h', quick: true },
  { value: '12h', label: '12h' },
  { value: '24h', label: '24h', quick: true },
  { value: '2d', label: '2d' },
  { value: '3d', label: '3d', quick: true },
  { value: '5d', label: '5d' },
  { value: '1w', label: '1w', quick: true },
  { value: '2w', label: '2w' },
  { value: '1m', label: '1m' },
  { value: 'all', label: 'all' },
]
