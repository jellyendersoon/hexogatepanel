/**
 * White-label brand of the host the dashboard is opened on.
 * The backend injects it into index.html for a tenant's own domain
 * (see app/utils/brand.py); on every other host it is absent and the
 * built-in Hexogate presentation is used.
 */
export interface BrandTheme {
  defaultMode: 'light' | 'dark' | null
  themeColor: string | null
  /** CSS custom properties laid over the default palette, per mode. */
  light: Record<string, string>
  dark: Record<string, string>
}

export interface Brand {
  key: string
  name: string
  logo: string | null
  logoOnLight: string | null
  favicon: string | null
  theme?: BrandTheme | null
}

declare global {
  interface Window {
    __HEXO_BRAND__?: Brand
  }
}

export const brand: Brand | null = (typeof window !== 'undefined' && window.__HEXO_BRAND__) || null

/** Brand name, or the given default on an unbranded host. */
export const brandName = (fallback: string): string => brand?.name ?? fallback

/** Logo for the current theme, or the given default on an unbranded host. */
export const brandLogo = (isDark: boolean, fallback: string): string => (brand && (isDark ? brand.logo : brand.logoOnLight)) || fallback

/** A brand colour token (dark palette) as a CSS colour, for the few surfaces that are not theme-driven. */
export const brandColor = (token: string, alpha = 1): string | undefined => {
  const value = brand?.theme?.dark?.[token]
  return value ? `hsl(${value} / ${alpha})` : undefined
}
