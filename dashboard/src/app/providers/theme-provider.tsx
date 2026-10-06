import { useEffect, useState, useCallback, useMemo } from 'react'
import { colorThemes, composeTheme, LEGACY_COLOR_THEME, LEGACY_THEME_TO_BASE, type ColorTheme } from '@/constants/color-themes'
import { applyThemeCustomization, DEFAULT_THEME_CUSTOMIZATION, parseThemeCustomization, type ThemeCustomization } from '@/lib/theme-color'
import { parseRadius, ThemeProviderContext, type Radius, type Theme, type ThemeProviderState } from '@/app/providers/theme-context'

type ThemeProviderProps = {
  children: React.ReactNode
  defaultTheme?: Theme
  defaultColorTheme?: ColorTheme
  defaultRadius?: Radius
  storageKey?: string
  colorStorageKey?: string
  radiusStorageKey?: string
  customizationStorageKey?: string
}

const VALID_THEMES: Theme[] = ['light', 'dark', 'system']

const safeLocalStorage = {
  getItem: (key: string): string | null => {
    try {
      return localStorage.getItem(key)
    } catch (error) {
      console.warn(`Failed to get localStorage item ${key}:`, error)
      return null
    }
  },
  setItem: (key: string, value: string): boolean => {
    try {
      localStorage.setItem(key, value)
      return true
    } catch (error) {
      console.warn(`Failed to set localStorage item ${key}:`, error)
      return false
    }
  },
  removeItem: (key: string): boolean => {
    try {
      localStorage.removeItem(key)
      return true
    } catch (error) {
      console.warn(`Failed to remove localStorage item ${key}:`, error)
      return false
    }
  },
}

const applyThemeVars = (vars: Record<string, string>) => {
  const root = document.documentElement
  Object.entries(vars).forEach(([key, value]) => {
    root.style.setProperty(key, value)
  })
}

const getSystemTheme = (): 'light' | 'dark' => {
  if (typeof window === 'undefined') return 'light'
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

export function ThemeProvider({
  children,
  defaultTheme = 'system',
  defaultColorTheme = 'default',
  defaultRadius = '0.5rem',
  storageKey = 'theme',
  colorStorageKey = 'color-theme',
  radiusStorageKey = 'radius',
  customizationStorageKey = 'theme-customization',
  ...props
}: ThemeProviderProps) {
  const [theme, setThemeState] = useState<Theme>(() => {
    const saved = safeLocalStorage.getItem(storageKey) as Theme
    return saved && VALID_THEMES.includes(saved) ? saved : defaultTheme
  })

  const [colorTheme, setColorThemeState] = useState<ColorTheme>(() => {
    const saved = safeLocalStorage.getItem(colorStorageKey)
    if (saved && LEGACY_THEME_TO_BASE[saved]) return 'default'
    const migrated = saved ? (LEGACY_COLOR_THEME[saved] ?? saved) : saved
    return migrated && Object.keys(colorThemes).includes(migrated) ? (migrated as ColorTheme) : defaultColorTheme
  })

  const [radius, setRadiusState] = useState<Radius>(() => parseRadius(safeLocalStorage.getItem(radiusStorageKey), defaultRadius))

  const [customization, setCustomizationState] = useState<ThemeCustomization>(() => {
    const parsed = parseThemeCustomization(safeLocalStorage.getItem(customizationStorageKey))
    const savedColor = safeLocalStorage.getItem(colorStorageKey)
    const legacyBase = savedColor ? LEGACY_THEME_TO_BASE[savedColor] : undefined
    if (legacyBase && parsed.baseColor === 'default') {
      return { ...parsed, baseColor: legacyBase }
    }
    return parsed
  })

  const [resolvedTheme, setResolvedTheme] = useState<'light' | 'dark'>(() => {
    return theme === 'system' ? getSystemTheme() : theme === 'dark' ? 'dark' : 'light'
  })

  const persistCustomization = useCallback(
    (next: ThemeCustomization) => {
      setCustomizationState(next)
      if (!safeLocalStorage.setItem(customizationStorageKey, JSON.stringify(next))) {
        console.warn('Failed to save theme customization to localStorage, changes may not persist')
      }
    },
    [customizationStorageKey],
  )

  const applyTheme = useCallback((themeMode: 'light' | 'dark', colorThemeName: ColorTheme, radiusValue: Radius, customizationValue: ThemeCustomization) => {
    const root = document.documentElement

    root.classList.remove('light', 'dark')
    root.classList.add(themeMode)
    root.dataset.density = customizationValue.density
    root.dataset.surface = customizationValue.surface
    root.dataset.style = customizationValue.style

    applyThemeVars({
      ...applyThemeCustomization(composeTheme(customizationValue.baseColor, colorThemeName, themeMode), customizationValue),
      '--radius': radiusValue,
    })
  }, [])

  useEffect(() => {
    const mediaQuery = window.matchMedia('(prefers-color-scheme: dark)')

    const handleChange = (e: MediaQueryListEvent) => {
      if (theme === 'system') {
        const systemTheme = e.matches ? 'dark' : 'light'
        setResolvedTheme(systemTheme)
        applyTheme(systemTheme, colorTheme, radius, customization)
      }
    }

    mediaQuery.addEventListener('change', handleChange)
    return () => mediaQuery.removeEventListener('change', handleChange)
  }, [theme, colorTheme, radius, customization, applyTheme])

  useEffect(() => {
    const newResolvedTheme = theme === 'system' ? getSystemTheme() : theme === 'dark' ? 'dark' : 'light'
    setResolvedTheme(newResolvedTheme)
    applyTheme(newResolvedTheme, colorTheme, radius, customization)
  }, [theme, colorTheme, radius, customization, applyTheme])

  const setTheme = useCallback(
    (newTheme: Theme) => {
      if (!safeLocalStorage.setItem(storageKey, newTheme)) {
        console.warn('Failed to save theme to localStorage, changes may not persist')
      }
      setThemeState(newTheme)
    },
    [storageKey],
  )

  const setColorTheme = useCallback(
    (newColorTheme: ColorTheme) => {
      if (!Object.keys(colorThemes).includes(newColorTheme)) {
        console.warn(`Invalid color theme: ${newColorTheme}`)
        return
      }
      if (!safeLocalStorage.setItem(colorStorageKey, newColorTheme)) {
        console.warn('Failed to save color theme to localStorage, changes may not persist')
      }
      setColorThemeState(newColorTheme)
    },
    [colorStorageKey],
  )

  const setRadius = useCallback(
    (newRadius: Radius) => {
      const parsed = parseRadius(newRadius, radius)
      if (!safeLocalStorage.setItem(radiusStorageKey, parsed)) {
        console.warn('Failed to save radius to localStorage, changes may not persist')
      }
      setRadiusState(parsed)
    },
    [radiusStorageKey, radius],
  )

  const setCustomization = useCallback(
    (patch: Partial<ThemeCustomization>) => {
      persistCustomization({
        ...customization,
        ...patch,
      })
    },
    [customization, persistCustomization],
  )

  const resetToDefaults = useCallback(() => {
    safeLocalStorage.removeItem(storageKey)
    safeLocalStorage.removeItem(colorStorageKey)
    safeLocalStorage.removeItem(radiusStorageKey)
    safeLocalStorage.removeItem(customizationStorageKey)

    setThemeState(defaultTheme)
    setColorThemeState(defaultColorTheme)
    setRadiusState(defaultRadius)
    setCustomizationState({ ...DEFAULT_THEME_CUSTOMIZATION })
  }, [storageKey, colorStorageKey, radiusStorageKey, customizationStorageKey, defaultTheme, defaultColorTheme, defaultRadius])

  const value: ThemeProviderState = useMemo(
    () => ({
      theme,
      colorTheme,
      radius,
      customization,
      resolvedTheme,
      setTheme,
      setColorTheme,
      setRadius,
      setCustomization,
      resetToDefaults,
      isSystemTheme: theme === 'system',
    }),
    [theme, colorTheme, radius, customization, resolvedTheme, setTheme, setColorTheme, setRadius, setCustomization, resetToDefaults],
  )

  return (
    <ThemeProviderContext.Provider {...props} value={value}>
      {children}
    </ThemeProviderContext.Provider>
  )
}
