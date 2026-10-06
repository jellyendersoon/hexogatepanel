import { createContext, useContext } from 'react'
import { colorThemes, type ColorTheme, type BaseColor } from '@/constants/color-themes'
import { DEFAULT_THEME_CUSTOMIZATION, type ThemeCustomization, type ThemeDensity, type ThemeNeutral, type ThemeSurface } from '@/lib/theme-color'

export type Theme = 'dark' | 'light' | 'system'
export type Radius = string
export type { ColorTheme, BaseColor, ThemeCustomization, ThemeDensity, ThemeNeutral, ThemeSurface }

export type ThemeProviderState = {
  theme: Theme
  colorTheme: ColorTheme
  radius: Radius
  customization: ThemeCustomization
  resolvedTheme: 'light' | 'dark'
  setTheme: (theme: Theme) => void
  setColorTheme: (colorTheme: ColorTheme) => void
  setRadius: (radius: Radius) => void
  setCustomization: (patch: Partial<ThemeCustomization>) => void
  resetToDefaults: () => void
  isSystemTheme: boolean
}

const initialState: ThemeProviderState = {
  theme: 'system',
  colorTheme: 'default',
  radius: '0.5rem',
  customization: DEFAULT_THEME_CUSTOMIZATION,
  resolvedTheme: 'light',
  setTheme: () => null,
  setColorTheme: () => null,
  setRadius: () => null,
  setCustomization: () => null,
  resetToDefaults: () => null,
  isSystemTheme: true,
}

export const ThemeProviderContext = createContext<ThemeProviderState>(initialState)

const RADIUS_MIN = 0
const RADIUS_MAX = 1.5

export function parseRadius(value: string | null, fallback: Radius = '0.5rem'): Radius {
  if (value === '0') return '0'
  if (!value) return fallback
  const match = value.trim().match(/^([\d.]+)rem$/)
  if (!match) return fallback
  const amount = Number(match[1])
  if (Number.isNaN(amount) || amount < RADIUS_MIN || amount > RADIUS_MAX) return fallback
  return `${amount}rem`
}

export function formatRadius(value: number): Radius {
  const amount = Math.round(Math.min(RADIUS_MAX, Math.max(RADIUS_MIN, value)) * 100) / 100
  return amount === 0 ? '0' : `${amount}rem`
}

export const useTheme = () => {
  const context = useContext(ThemeProviderContext)
  if (context === undefined) {
    throw new Error('useTheme must be used within a ThemeProvider')
  }
  return context
}

export { colorThemes }
export { DEFAULT_THEME_CUSTOMIZATION } from '@/lib/theme-color'
