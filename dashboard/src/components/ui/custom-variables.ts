export interface CustomVariableDefinition {
  key: string
  value?: string
}

export const CUSTOM_VARIABLE_SAMPLE_VALUES: Record<string, string | number> = {
  SERVER_IP: '203.0.113.10',
  SERVER_IPV6: '[2001:db8::10]',
  USERNAME: 'alice',
  DATA_USAGE: '2 GB',
  DATA_LEFT: '8 GB',
  DATA_LIMIT: '10 GB',
  DAYS_LEFT: '14',
  EXPIRE_DATE: '2026-12-31',
  JALALI_EXPIRE_DATE: '1405-10-10',
  TIME_LEFT: '14d',
  STATUS_EMOJI: 'OK',
  USAGE_PERCENTAGE: '20',
  ADMIN_USERNAME: 'admin',
  PROFILE_TITLE: 'Alice Profile',
  PROTOCOL: 'vless',
  TRANSPORT: 'ws',
  url: 'https://example.com/sub/alice',
  format: 'links',
}

export function normalizeCustomVariableKey(value: string) {
  const stripped = value.trim().replace(/^\{|\}$/g, '')
  return stripped.toUpperCase().replace(/[^A-Z0-9_]/g, '_')
}

export function previewVariableValue(value: string, variables: Record<string, string | number> = CUSTOM_VARIABLE_SAMPLE_VALUES) {
  return value.replace(/\{([A-Za-z0-9_]+)\}/g, (_, key: string) => {
    const replacement = variables[key]
    return replacement == null ? '<missing>' : String(replacement)
  })
}
