import type { SettingsSchema } from '@/service/api'
import { createContext, useContext } from 'react'

// Settings payload accepted by updateSettings: either plain settings or already wrapped as { data }
export type SettingsUpdateInput = SettingsSchema & { data?: SettingsSchema }

// Create context for settings
export interface SettingsContextType {
  settings: SettingsSchema
  isLoading: boolean
  error: unknown
  updateSettings: (data: SettingsUpdateInput) => Promise<void>
  isSaving: boolean
}

export const SettingsContext = createContext<SettingsContextType | undefined>(undefined)

export const useSettingsContext = () => {
  const context = useContext(SettingsContext)
  if (!context) {
    throw new Error('useSettingsContext must be used within SettingsProvider')
  }
  return context!
}
