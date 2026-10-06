import { useAdmin } from '@/hooks/use-admin'
import { hasPermission } from '@/utils/rbac'
import { Navigate } from 'react-router'

// Component to handle default settings routing based on user permissions
export function SettingsIndex() {
  const { admin } = useAdmin()
  const canUpdateSettings = hasPermission(admin, 'settings', 'update')
  const canSeeGeneral = hasPermission(admin, 'settings', 'read_general') && canUpdateSettings
  const defaultPath = canSeeGeneral ? '/settings/general' : '/settings/theme'

  return <Navigate to={defaultPath} replace />
}

export function TemplatesIndex() {
  const { admin } = useAdmin()
  const defaultPath = hasPermission(admin, 'templates', 'read') ? '/templates/user' : hasPermission(admin, 'client_templates', 'read') ? '/templates/client' : '/settings/theme'

  return <Navigate to={defaultPath} replace />
}
