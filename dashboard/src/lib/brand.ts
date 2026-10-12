/**
 * White-label brand of the host the dashboard is opened on.
 * The backend injects it into index.html for a tenant's own domain
 * (see app/utils/brand.py); on every other host it is absent and the
 * built-in Hexogate presentation is used.
 */
export interface Brand {
  key: string
  name: string
  logo: string | null
  logoOnLight: string | null
  favicon: string | null
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
