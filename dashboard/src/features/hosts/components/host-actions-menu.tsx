import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { Button } from '@/components/ui/button'
import { Copy, MoreVertical, Pencil, Power, PowerOff, Trash2 } from 'lucide-react'
import { BaseHost, modifyHost, removeHost, type MuxSettingsInput } from '@/service/api'
import { toast } from 'sonner'
import useDirDetection from '@/hooks/use-dir-detection'
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from '@/components/ui/alert-dialog'
import { cn } from '@/lib/utils'
import { escapeHtml } from '@/utils/escape-html'

interface HostActionsMenuProps {
  host: BaseHost
  onEdit: (host: BaseHost) => void
  onDuplicate: (host: BaseHost) => Promise<void>
  onDataChanged?: () => void
  canUpdate?: boolean
  canCreate?: boolean
  className?: string
}

const DeleteAlertDialog = ({ host, isOpen, onClose, onConfirm }: { host: BaseHost; isOpen: boolean; onClose: () => void; onConfirm: () => void }) => {
  const { t } = useTranslation()
  const dir = useDirDetection()

  return (
    <AlertDialog open={isOpen} onOpenChange={onClose}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{t('deleteHost.title')}</AlertDialogTitle>
          <AlertDialogDescription>
            <span dir={dir} dangerouslySetInnerHTML={{ __html: t('deleteHost.prompt', { name: escapeHtml(host.remark ?? '') }) }} />
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel onClick={onClose}>{t('cancel')}</AlertDialogCancel>
          <AlertDialogAction variant="destructive" onClick={onConfirm}>
            {t('delete')}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}

export default function HostActionsMenu({ host, onEdit, onDuplicate, onDataChanged, canUpdate = true, canCreate = true, className }: HostActionsMenuProps) {
  const [isDeleteDialogOpen, setDeleteDialogOpen] = useState<boolean>(false)
  const { t } = useTranslation()
  const dir = useDirDetection()

  const handleToggleStatus = async () => {
    const { id, ...hostData } = host
    if (!canUpdate || !id) return

    try {
      let transformedMuxSettings: MuxSettingsInput | null | undefined = hostData.mux_settings
      if (hostData.mux_settings?.xray) {
        transformedMuxSettings = {
          ...hostData.mux_settings,
          xray: {
            enabled: hostData.mux_settings.xray.enabled,
            concurrency: hostData.mux_settings.xray.concurrency,
            xudp_concurrency: hostData.mux_settings.xray.xudpConcurrency ?? undefined,
            xudp_proxy_udp_443: hostData.mux_settings.xray.xudpProxyUDP443 ?? undefined,
          },
        }
      }

      await modifyHost(id, {
        ...hostData,
        mux_settings: transformedMuxSettings,
        transport_settings: hostData.transport_settings,
        is_disabled: !host.is_disabled,
      })

      toast.success(
        t(host.is_disabled ? 'host.enableSuccess' : 'host.disableSuccess', {
          name: host.remark ?? '',
          defaultValue: `Host "{name}" has been ${host.is_disabled ? 'enabled' : 'disabled'} successfully`,
        }),
      )

      if (onDataChanged) {
        onDataChanged()
      }
    } catch {
      toast.error(
        t(host.is_disabled ? 'host.enableFailed' : 'host.disableFailed', {
          name: host.remark ?? '',
          defaultValue: `Failed to ${host.is_disabled ? 'enable' : 'disable'} host "{name}"`,
        }),
      )
    }
  }

  const handleDeleteClick = (event: Event) => {
    event.preventDefault()
    event.stopPropagation()
    setDeleteDialogOpen(true)
  }

  const handleConfirmDelete = async () => {
    if (!canUpdate || !host.id) return

    try {
      await removeHost(host.id)

      toast.success(
        t('deleteHost.deleteSuccess', {
          name: host.remark ?? '',
          defaultValue: 'Host "{name}" removed successfully',
        }),
      )

      setDeleteDialogOpen(false)

      if (onDataChanged) {
        onDataChanged()
      }
    } catch {
      toast.error(
        t('deleteHost.deleteFailed', {
          name: host.remark ?? '',
          defaultValue: 'Failed to remove host "{name}"',
        }),
      )
    }
  }

  if (!canUpdate && !canCreate) return null

  return (
    <div className={cn(className)} onClick={e => e.stopPropagation()}>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button type="button" variant="ghost" size="icon">
            <MoreVertical className="h-4 w-4" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align={dir === 'rtl' ? 'start' : 'end'}>
          {canUpdate && (
            <>
              <DropdownMenuItem
                onSelect={e => {
                  e.stopPropagation()
                  onEdit(host)
                }}
              >
                <Pencil className={cn('h-4 w-4', dir === 'rtl' ? 'ml-2' : 'mr-2')} />
                {t('edit')}
              </DropdownMenuItem>
              <DropdownMenuItem
                onSelect={e => {
                  e.stopPropagation()
                  handleToggleStatus()
                }}
              >
                {host?.is_disabled ? <Power className={cn('h-4 w-4', dir === 'rtl' ? 'ml-2' : 'mr-2')} /> : <PowerOff className={cn('h-4 w-4', dir === 'rtl' ? 'ml-2' : 'mr-2')} />}
                {host?.is_disabled ? t('enable') : t('disable')}
              </DropdownMenuItem>
            </>
          )}
          {canCreate && (
            <DropdownMenuItem
              onSelect={e => {
                e.stopPropagation()
                onDuplicate(host)
              }}
            >
              <Copy className={cn('h-4 w-4', dir === 'rtl' ? 'ml-2' : 'mr-2')} />
              {t('duplicate')}
            </DropdownMenuItem>
          )}
          {canUpdate && <DropdownMenuSeparator />}
          {canUpdate && (
            <DropdownMenuItem onSelect={handleDeleteClick} className="text-destructive">
              <Trash2 className={cn('h-4 w-4', dir === 'rtl' ? 'ml-2' : 'mr-2')} />
              {t('delete')}
            </DropdownMenuItem>
          )}
        </DropdownMenuContent>
      </DropdownMenu>

      {canUpdate && <DeleteAlertDialog host={host} isOpen={isDeleteDialogOpen} onClose={() => setDeleteDialogOpen(false)} onConfirm={handleConfirmDelete} />}
    </div>
  )
}
