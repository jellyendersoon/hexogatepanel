import { toast } from 'sonner'
import { useCallback } from 'react'
import { useTranslation } from 'react-i18next'
import { isEmptyObject } from '@/utils/isEmptyObject.ts'

interface DynamicErrorForm {
  clearErrors(): void
  setError(name: string, error: { type: string; message?: string }): void
}

interface DynamicErrorHandlerProps {
  error: unknown
  fields: string[]
  form: DynamicErrorForm
  contextKey: string
}

type ErrorResponseData = { detail?: unknown; message?: unknown } | string | null | undefined

interface ApiErrorLike {
  response?: { _data?: unknown; data?: unknown }
  data?: unknown
  message?: unknown
}

interface ValidationErrorItem {
  loc?: unknown[]
  msg?: string
}

// Hook for handling error
const useDynamicErrorHandler = () => {
  const { t } = useTranslation()

  return useCallback(
    ({ error, fields, form, contextKey }: DynamicErrorHandlerProps) => {
      const apiError = error as ApiErrorLike | null | undefined
      console.error('Operation failed:', error)
      console.error('Error response:', apiError?.response)
      // Reset all previous errors
      form.clearErrors()

      const responseData = (apiError?.response?._data || apiError?.response?.data || apiError?.data) as ErrorResponseData

      // Handle validation errors
      if (responseData && !isEmptyObject(typeof responseData === 'string' ? null : responseData)) {
        const detail = typeof responseData === 'object' ? responseData.detail : undefined

        if (Array.isArray(detail)) {
          const validationErrors = detail as ValidationErrorItem[]
          validationErrors.forEach(err => {
            const field = err?.loc?.[1]
            if (typeof field === 'string' && field && fields.includes(field)) {
              form.setError(field, {
                type: 'manual',
                message: err.msg,
              })
            }
          })

          const firstError = validationErrors[0]
          const firstPath = Array.isArray(firstError?.loc) ? firstError.loc.filter((part: unknown) => part !== 'body').join('.') : ''
          toast.error(firstError?.msg ? `${firstPath ? `${firstPath}: ` : ''}${firstError.msg}` : 'Validation error')
        } else if (typeof detail === 'object' && detail !== null) {
          const detailRecord = detail as Record<string, unknown>
          const firstField = Object.keys(detailRecord)[0]
          const firstMessage = detailRecord[firstField]

          Object.entries(detailRecord).forEach(([field, message]) => {
            if (fields.includes(field)) {
              form.setError(field, {
                type: 'manual',
                message:
                  typeof message === 'string'
                    ? message
                    : t('validation.invalid', {
                        field: t(`${contextKey}.${field}`, { defaultValue: field }),
                        defaultValue: `${field} is invalid`,
                      }),
              })
            }
          })

          toast.error(
            (typeof firstMessage === 'string' && firstMessage) ||
              t('validation.invalid', {
                field: t(`${contextKey}.${firstField}`, { defaultValue: firstField }),
                defaultValue: `${firstField} is invalid`,
              }),
          )
        } else if (typeof detail === 'string') {
          toast.error(detail)
        }
      } else if (responseData) {
        // Handle structured API errors
        let errorMessage = ''

        if (typeof responseData === 'string') {
          errorMessage = responseData
        } else if (Array.isArray(responseData.detail)) {
          // Pydantic-style array of errors
          const validationErrors = responseData.detail as ValidationErrorItem[]
          validationErrors.forEach(err => {
            const field = err?.loc?.[1]
            if (typeof field === 'string' && field) {
              form.setError(field, {
                type: 'manual',
                message: err.msg,
              })
            }
          })
          errorMessage = validationErrors[0]?.msg || 'Validation error'
        } else if (typeof responseData.detail === 'string') {
          errorMessage = responseData.detail
        } else if (typeof responseData.message === 'string' && responseData.message) {
          errorMessage = responseData.message
        } else {
          errorMessage = 'An unexpected error occurred'
        }

        toast.error(errorMessage)
      } else {
        // Generic fallback
        toast.error((typeof apiError?.message === 'string' && apiError.message) || t(`${contextKey}.genericError`, { defaultValue: 'An error occurred' }))
      }
    },
    [t],
  )
}

export default useDynamicErrorHandler
