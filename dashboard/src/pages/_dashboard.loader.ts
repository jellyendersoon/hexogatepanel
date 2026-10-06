import { getCurrentAdmin, type AdminDetails } from '@/service/api'
import { isAuthenticationError } from '@/utils/error-utils'

export const clientLoader = async (): Promise<AdminDetails> => {
  try {
    const response = await getCurrentAdmin()
    return response
  } catch (error) {
    if (isAuthenticationError(error)) {
      throw Response.redirect('/login')
    }

    throw error
  }
}
