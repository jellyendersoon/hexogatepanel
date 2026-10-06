import { useCallback, useMemo, useState } from 'react'
import Cores from '@/features/nodes/components/cores/cores-list'
import { useGetAllCores, useDeleteCoreConfig, useCreateCoreConfig } from '@/service/api'
import { toast } from 'sonner'
import { useTranslation } from 'react-i18next'
import { AlertDialog, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from '@/components/ui/alert-dialog'
import { LoaderButton } from '@/components/ui/loader-button'
import { useQueryClient } from '@tanstack/react-query'
import { AlertTriangle } from 'lucide-react'
import { useAdmin } from '@/hooks/use-admin'
import { hasPermission } from '@/utils/rbac'
import { escapeHtml } from '@/utils/escape-html'

export default function CoresIndexPage() {
  const { admin } = useAdmin()
  const canCreateCores = hasPermission(admin, 'cores', 'create')
  const canUpdateCores = hasPermission(admin, 'cores', 'update')
  const canDeleteCores = hasPermission(admin, 'cores', 'delete')
  const { data: coresData } = useGetAllCores({})
  const queryClient = useQueryClient()
  const deleteCoreConfig = useDeleteCoreConfig()
  const createCoreMutation = useCreateCoreConfig()
  const { t } = useTranslation()
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false)
  const [coreToDelete, setCoreToDelete] = useState<string | null>(null)
  const [coreIdToDelete, setCoreIdToDelete] = useState<number | null>(null)

  const handleDuplicateCore = useCallback(
    (coreId: string | number) => {
      if (!canCreateCores) return

      const numericCoreId = Number(coreId)
      const coreToDuplicate = coresData?.cores?.find(core => core.id === numericCoreId)

      if (!coreToDuplicate) {
        toast.error(t('settings.cores.coreNotFound'))
        return
      }

      try {
        const newCore = {
          ...coreToDuplicate,
          id: undefined,
          name: `${coreToDuplicate.name} (Copy)`,
        }

        createCoreMutation.mutateAsync(
          {
            data: newCore,
          },
          {
            onSuccess: () => {
              toast.success(
                t('settings.cores.duplicateSuccess', {
                  name: coreToDuplicate.name,
                }),
              )
              queryClient.invalidateQueries({ queryKey: ['/api/cores'] })
              queryClient.invalidateQueries({ queryKey: ['/api/cores/simple'] })
            },
            onError: error => {
              toast.error(
                error.message ||
                  t('settings.cores.duplicateFailed', {
                    name: coreToDuplicate.name,
                  }),
              )
            },
          },
        )
      } catch {
        toast.error(
          t('settings.cores.duplicateFailed', {
            name: coreToDuplicate.name,
          }),
        )
      }
    },
    [canCreateCores, coresData?.cores, createCoreMutation, queryClient, t],
  )

  const handleDeleteCore = useCallback(
    (coreName: string, coreId: number) => {
      if (!canDeleteCores) return
      setCoreToDelete(coreName)
      setCoreIdToDelete(coreId)
      setDeleteDialogOpen(true)
    },
    [canDeleteCores],
  )

  const confirmDeleteCore = useCallback(() => {
    if (!canDeleteCores || !coreToDelete || coreIdToDelete === null) return

    deleteCoreConfig.mutate(
      {
        coreId: coreIdToDelete,
        params: { restart_nodes: true },
      },
      {
        onSuccess: () => {
          toast.success(
            t('settings.cores.deleteSuccess', {
              name: `Core ${coreToDelete}`,
            }),
          )
          setDeleteDialogOpen(false)
          setCoreToDelete(null)
          queryClient.invalidateQueries({ queryKey: ['/api/cores'] })
          queryClient.invalidateQueries({ queryKey: ['/api/cores/simple'] })
        },
        onError: (error: unknown) => {
          let errorMessage = t('settings.cores.deleteFailed', {
            name: `Core ${coreToDelete}`,
          })

          const maybeError = typeof error === 'object' && error !== null ? (error as { response?: { _data?: unknown; data?: unknown }; data?: unknown; message?: unknown }) : undefined
          const responseData = maybeError?.response?._data || maybeError?.response?.data || maybeError?.data
          const detail = typeof responseData === 'object' && responseData !== null && 'detail' in responseData ? responseData.detail : undefined
          if (detail) {
            if (typeof detail === 'string') {
              errorMessage = detail
            } else if (Array.isArray(detail) && detail.length > 0) {
              const firstError: unknown = detail[0]
              const firstMessage = typeof firstError === 'object' && firstError !== null && 'msg' in firstError ? firstError.msg : undefined
              errorMessage = (typeof firstMessage === 'string' && firstMessage) || (typeof firstError === 'string' && firstError) || errorMessage
            }
          } else if (typeof maybeError?.message === 'string' && maybeError.message) {
            errorMessage = maybeError.message
          }

          toast.error(errorMessage)
          setDeleteDialogOpen(false)
          setCoreToDelete(null)
        },
      },
    )
  }, [canDeleteCores, coreToDelete, coreIdToDelete, deleteCoreConfig, queryClient, t])

  const handleDeleteDialogClose = useCallback(() => {
    setDeleteDialogOpen(false)
    setCoreToDelete(null)
    setCoreIdToDelete(null)
  }, [])

  const cores = useMemo(() => coresData?.cores ?? [], [coresData?.cores])

  return (
    <div className="flex flex-col px-4">
      <Cores
        cores={cores}
        onDuplicateCore={canCreateCores ? handleDuplicateCore : undefined}
        onDeleteCore={canDeleteCores ? handleDeleteCore : undefined}
        canCreate={canCreateCores}
        canUpdate={canUpdateCores}
        canDelete={canDeleteCores}
      />

      <AlertDialog open={deleteDialogOpen} onOpenChange={handleDeleteDialogClose}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle className="flex items-center gap-2">
              <AlertTriangle className="text-destructive h-5 w-5" />
              {t('settings.cores.delete')}
            </AlertDialogTitle>
            <AlertDialogDescription>
              <span dangerouslySetInnerHTML={{ __html: t('core.deleteConfirm', { name: escapeHtml(coreToDelete) }) }} />
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel onClick={handleDeleteDialogClose} disabled={deleteCoreConfig.isPending}>
              {t('cancel')}
            </AlertDialogCancel>
            <LoaderButton variant="destructive" onClick={confirmDeleteCore} disabled={deleteCoreConfig.isPending} isLoading={deleteCoreConfig.isPending} loadingText={t('removing')}>
              {t('delete')}
            </LoaderButton>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
