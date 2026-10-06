/**
 * Escape a value for interpolation into translated strings that are rendered
 * as HTML (dangerouslySetInnerHTML). i18next interpolation is configured with
 * escapeValue: false so that plain-text renders are not entity-encoded; any
 * value that ends up inside HTML markup must go through this instead.
 */
export function escapeHtml(value: unknown): string {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}
