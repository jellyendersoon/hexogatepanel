import MainSection from '@/features/hosts/components/hosts-list'
import { type HostFormValues } from '@/features/hosts/forms/host-form'
import PageHeader from '@/components/layout/page-header'
import { Separator } from '@/components/ui/separator'
import { BaseHost, createHost, CreateHost, getHosts, modifyHost, MultiplexProtocol, ProxyHostALPN, ProxyHostFingerprint, Xudp } from '@/service/api'
import { useAdmin } from '@/hooks/use-admin'
import { hasPermission } from '@/utils/rbac'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Plus } from 'lucide-react'
import { useCallback, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import { useCommandCreate } from '@/hooks/use-command-create'

export default function HostsPage() {
  const { admin } = useAdmin()
  const canCreateHosts = hasPermission(admin, 'hosts', 'create')
  const canUpdateHosts = hasPermission(admin, 'hosts', 'update')
  const [isDialogOpen, setIsDialogOpen] = useState(false)
  const [editingHost, setEditingHost] = useState<BaseHost | null>(null)
  const { data, refetch, isFetching } = useQuery({
    queryKey: ['getGetHostsQueryKey'],
    queryFn: () => getHosts(),
  })
  const { t } = useTranslation()
  const queryClient = useQueryClient()

  const handleDialogOpen = (open: boolean) => {
    setIsDialogOpen(open)
    if (!open) {
      setEditingHost(null)
    }
  }

  const handleCreateClick = useCallback(() => {
    if (!canCreateHosts) return
    setEditingHost(null)
    setIsDialogOpen(true)
  }, [canCreateHosts])

  useCommandCreate('host', handleCreateClick)

  const onAddHost = (open: boolean) => {
    setIsDialogOpen(open)
  }

  const handleSubmit = async (formData: HostFormValues) => {
    try {
      if (editingHost?.id && !canUpdateHosts) {
        toast.error(t('permissionDenied', { defaultValue: 'Permission denied' }))
        return { status: 403 }
      }
      if (!editingHost?.id && !canCreateHosts) {
        toast.error(t('permissionDenied', { defaultValue: 'Permission denied' }))
        return { status: 403 }
      }

      // Check if all protocols are set to none
      const allProtocolsNone =
        formData.mux_settings &&
        (!formData.mux_settings.sing_box?.protocol || formData.mux_settings.sing_box.protocol === 'none') &&
        (!formData.mux_settings.clash?.protocol || formData.mux_settings.clash.protocol === 'none') &&
        !formData.mux_settings.xray?.concurrency

      // If creating a new host, set priority to max+1 (or 0 if no hosts exist yet)
      let priority = formData.priority
      if (!editingHost?.id) {
        const maxPriority = data && data.length > 0 ? Math.max(...data.map(h => h.priority ?? 0)) : -1
        priority = maxPriority + 1
      }

      const { ech_config_list, ech_query_strategy, mihomo_ech_config, mihomo_ech_query_server_name, sing_box_ech_config, sing_box_ech_query_server_name, ...hostFields } = formData
      const ech =
        ech_config_list || ech_query_strategy || mihomo_ech_config || mihomo_ech_query_server_name || sing_box_ech_config || sing_box_ech_query_server_name
          ? {
              xray:
                ech_config_list || ech_query_strategy
                  ? {
                      config_list: ech_config_list || undefined,
                      query_strategy: ech_query_strategy || undefined,
                    }
                  : undefined,
              mihomo:
                mihomo_ech_config || mihomo_ech_query_server_name
                  ? {
                      config: mihomo_ech_config || undefined,
                      query_server_name: mihomo_ech_query_server_name || undefined,
                    }
                  : undefined,
              sing_box:
                sing_box_ech_config || sing_box_ech_query_server_name
                  ? {
                      config: sing_box_ech_config || undefined,
                      query_server_name: sing_box_ech_query_server_name || undefined,
                    }
                  : undefined,
            }
          : undefined

      // Convert HostFormValues to CreateHost type
      const hostData: CreateHost = {
        ...hostFields,
        priority,
        alpn: formData.alpn as ProxyHostALPN[] | undefined,
        fingerprint: formData.fingerprint as ProxyHostFingerprint | undefined,
        ech,
        pinned_peer_cert_sha256: formData.pinned_peer_cert_sha256 || undefined,
        verify_peer_cert_by_name: formData.verify_peer_cert_by_name && formData.verify_peer_cert_by_name.length > 0 ? formData.verify_peer_cert_by_name : undefined,
        vless_route: formData.vless_route || undefined,
        transport_settings: formData.transport_settings
          ? {
              ...formData.transport_settings,
              xhttp_settings: formData.transport_settings.xhttp_settings
                ? {
                    ...formData.transport_settings.xhttp_settings,
                    xmux: formData.transport_settings.xhttp_settings.xmux
                      ? {
                          maxConcurrency: formData.transport_settings.xhttp_settings.xmux.max_concurrency || undefined,
                          maxConnections: formData.transport_settings.xhttp_settings.xmux.max_connections || undefined,
                          cMaxReuseTimes: formData.transport_settings.xhttp_settings.xmux.c_max_reuse_times || undefined,
                          hMaxReusableSecs: formData.transport_settings.xhttp_settings.xmux.h_max_reusable_secs || undefined,
                          hMaxRequestTimes: formData.transport_settings.xhttp_settings.xmux.h_max_request_times || undefined,
                          hKeepAlivePeriod: formData.transport_settings.xhttp_settings.xmux.h_keep_alive_period || undefined,
                        }
                      : undefined,
                  }
                : undefined,
            }
          : undefined,
        mux_settings: allProtocolsNone
          ? undefined
          : formData.mux_settings
            ? {
                ...formData.mux_settings,
                sing_box: formData.mux_settings.sing_box
                  ? {
                      enable: formData.mux_settings.sing_box.enable || false,
                      protocol: formData.mux_settings.sing_box.protocol === 'none' ? undefined : (formData.mux_settings.sing_box.protocol as MultiplexProtocol),
                      max_connections: formData.mux_settings.sing_box.max_connections || undefined,
                      max_streams: formData.mux_settings.sing_box.max_streams || undefined,
                      min_streams: formData.mux_settings.sing_box.min_streams || undefined,
                      padding: formData.mux_settings.sing_box.padding || undefined,
                      brutal: formData.mux_settings.sing_box.brutal || undefined,
                    }
                  : undefined,
                clash: formData.mux_settings.clash
                  ? {
                      enable: formData.mux_settings.clash.enable || false,
                      protocol: formData.mux_settings.clash.protocol === 'none' ? undefined : (formData.mux_settings.clash.protocol as MultiplexProtocol),
                      max_connections: formData.mux_settings.clash.max_connections || undefined,
                      max_streams: formData.mux_settings.clash.max_streams || undefined,
                      min_streams: formData.mux_settings.clash.min_streams || undefined,
                      padding: formData.mux_settings.clash.padding || undefined,
                      brutal: formData.mux_settings.clash.brutal || undefined,
                      statistic: formData.mux_settings.clash.statistic || undefined,
                      only_tcp: formData.mux_settings.clash.only_tcp || undefined,
                    }
                  : undefined,
                xray: formData.mux_settings.xray
                  ? {
                      enabled: formData.mux_settings.xray.enabled || false,
                      concurrency: formData.mux_settings.xray.concurrency || undefined,
                      xudp_concurrency: formData.mux_settings.xray.xudp_concurrency || undefined,
                      xudp_proxy_udp_443: formData.mux_settings.xray.xudp_proxy_443 === 'none' ? undefined : (formData.mux_settings.xray.xudp_proxy_443 as Xudp),
                    }
                  : undefined,
              }
            : undefined,
        fragment_settings: (() => {
          const xraySettings = formData.fragment_settings?.xray
            ? {
                packets: formData.fragment_settings.xray.packets || '',
                length: formData.fragment_settings.xray.length || '',
                interval: formData.fragment_settings.xray.interval || '',
              }
            : undefined

          const singboxSettings = formData.fragment_settings?.sing_box?.fragment
            ? {
                fragment: formData.fragment_settings.sing_box.fragment,
                fragment_fallback_delay: formData.fragment_settings.sing_box.fragment_fallback_delay || undefined,
                record_fragment: formData.fragment_settings.sing_box.record_fragment || undefined,
              }
            : undefined

          if (xraySettings || singboxSettings) {
            return {
              xray: xraySettings,
              sing_box: singboxSettings,
            }
          }
          return undefined
        })(),
        noise_settings: formData.noise_settings?.xray
          ? {
              xray: formData.noise_settings.xray.map(noise => ({
                type: noise.type,
                packet: noise.packet,
                delay: noise.delay,
                apply_to: noise.apply_to,
              })),
            }
          : undefined,
      }

      if (editingHost?.id) {
        // This is an edit operation
        await modifyHost(editingHost.id, hostData)
        return { status: 200 }
      } else {
        // This is a new host
        await createHost(hostData)
        return { status: 200 }
      }
    } catch (error) {
      const maybeError = typeof error === 'object' && error !== null ? (error as { response?: { _data?: unknown }; message?: unknown }) : undefined
      console.error('Error submitting host:', error)
      console.error('Error response:', maybeError?.response)
      console.error('Error data:', maybeError?.response?._data)

      let errorMessage = ''
      let errorField = ''

      const apiError = maybeError?.response?._data
      if (apiError) {
        const apiErrorRecord = typeof apiError === 'object' ? (apiError as { detail?: unknown; message?: unknown }) : undefined
        const detail = apiErrorRecord?.detail

        if (typeof apiError === 'string') {
          errorMessage = apiError
        } else if (detail) {
          if (Array.isArray(detail)) {
            // Get first error message from array
            const firstError = detail[0] as { loc?: unknown[]; msg?: unknown } | undefined
            const errorLocation = firstError?.loc?.[1]
            errorField = errorLocation ? String(errorLocation) : ''
            errorMessage = typeof firstError?.msg === 'string' && firstError.msg ? firstError.msg : 'Validation error'
          } else if (typeof detail === 'string') {
            errorMessage = detail
          } else if (typeof detail === 'object') {
            // Get first error message from object
            const firstError = Object.entries(detail)[0]
            errorField = firstError[0]
            errorMessage = typeof firstError[1] === 'string' ? firstError[1] : t('validation.invalid', { field: firstError[0] })
          } else {
            errorMessage = 'Validation error'
          }
        } else if (typeof apiErrorRecord?.message === 'string' && apiErrorRecord.message) {
          errorMessage = apiErrorRecord.message
        } else {
          errorMessage = t('hosts.genericError', { defaultValue: 'An unexpected error occurred' })
        }
      } else {
        errorMessage = (typeof maybeError?.message === 'string' && maybeError.message) || t('hosts.genericError', { defaultValue: 'An error occurred' })
      }

      // Show error message in toast with field name if available
      const toastMessage = errorField ? `${errorField}: ${errorMessage}` : errorMessage
      toast.error(toastMessage)
      return { status: 500 }
    } finally {
      // Refresh the hosts data
      queryClient.invalidateQueries({
        queryKey: ['/api/hosts'],
      })
    }
  }

  return (
    <div className="flex w-full flex-col items-start gap-2 pb-8">
      <div className="w-full transform-gpu">
        <PageHeader
          title="hosts"
          description="manageHosts"
          buttonIcon={canCreateHosts ? Plus : undefined}
          buttonText={canCreateHosts ? 'hostsDialog.addHost' : undefined}
          onButtonClick={canCreateHosts ? handleCreateClick : undefined}
        />
        <Separator />
      </div>

      <div className="w-full p-4">
        <MainSection
          data={data}
          isDialogOpen={isDialogOpen}
          onDialogOpenChange={handleDialogOpen}
          onAddHost={onAddHost}
          onSubmit={handleSubmit}
          editingHost={editingHost}
          setEditingHost={setEditingHost}
          onRefresh={refetch}
          isRefreshing={isFetching}
          canCreate={canCreateHosts}
          canUpdate={canUpdateHosts}
        />
      </div>
    </div>
  )
}
