import { useQuery } from '@tanstack/react-query'
import { orvalFetcher } from '@/service/http'

/** A master (reseller) and how many admins belong to it. id 0 = admins with no master. */
export interface AdminMaster {
  id: number
  username: string
  admins: number
}

export interface AdminMastersResponse {
  masters: AdminMaster[]
  total: number
}

export const ADMIN_MASTERS_QUERY_KEY = ['/api/admins/masters'] as const

export function useAdminMasters(enabled = true) {
  return useQuery({
    queryKey: ADMIN_MASTERS_QUERY_KEY,
    queryFn: ({ signal }) => orvalFetcher<AdminMastersResponse>('/api/admins/masters', { method: 'GET', signal }),
    enabled,
    staleTime: 0,
    retry: 1,
  })
}
