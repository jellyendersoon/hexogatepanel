import type { RolePermissionFormMap } from '@/features/admin-roles/forms/admin-role-form'

export function countEnabledPermissions(permissions?: RolePermissionFormMap | null): number {
  let count = 0
  for (const value of Object.values(permissions || {})) {
    if (!value || typeof value !== 'object') continue
    for (const inner of Object.values(value as Record<string, unknown>)) {
      if (inner === true) count += 1
      else if (inner && typeof inner === 'object' && 'scope' in inner && Number((inner as { scope?: unknown }).scope) > 0) count += 1
    }
  }
  return count
}
