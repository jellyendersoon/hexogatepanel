import type { XrayGeneratedFormField } from '@pasarguard/xray-config-kit'

/** Normalizes Go/JSON field names for `coreEditor.transportFields.<key>` labels. */
export function normalizeXrayParityFieldKey(field: XrayGeneratedFormField): string {
  return String(field.go || field.json || '')
    .replace(/[^a-zA-Z0-9]/g, '')
    .toLowerCase()
}

/** Product / protocol names spelled the same in every locale — omit from JSON, render as plain text. */
const TRANSPORT_FIELD_LOCALE_INVARIANT_LABELS: Readonly<Record<string, string>> = {
  spiderx: 'Spider X',
}

export function transportParityFieldLabel(field: XrayGeneratedFormField, t: (key: string, o?: { defaultValue?: string }) => string): string {
  const key = normalizeXrayParityFieldKey(field)
  const invariant = TRANSPORT_FIELD_LOCALE_INVARIANT_LABELS[key]
  if (invariant !== undefined) return invariant
  return t(`coreEditor.transportFields.${key}`, { defaultValue: field.go || field.json || '' })
}

export function isBooleanParityField(field: XrayGeneratedFormField): boolean {
  const key = normalizeXrayParityFieldKey(field)
  return field.type === 'bool' || key === 'xpaddingobfsmode'
}

export function isStringMapField(field: XrayGeneratedFormField): boolean {
  const key = normalizeXrayParityFieldKey(field)
  return key === 'requestheaders' || key === 'responseheaders' || key === 'headers' || key === 'attributes'
}

export function isWebhookField(field: XrayGeneratedFormField): boolean {
  const key = normalizeXrayParityFieldKey(field)
  return key === 'webhook'
}

export function isJsonRawMessageField(field: XrayGeneratedFormField): boolean {
  const key = normalizeXrayParityFieldKey(field)
  return key === 'obfuscationheaders' || key === 'headerconfig'
}
