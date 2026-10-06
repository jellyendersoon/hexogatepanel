import { ChevronDown } from 'lucide-react'
import { cn } from '@/lib/utils'
import useDirDetection from '@/hooks/use-dir-detection'

export function DataUsageHeader({ t, handleSort, filters }: { t: (key: string) => string; handleSort: (column: string, fromDropdown?: boolean) => void; filters: { sort: string } }) {
  const isRTL = useDirDetection() === 'rtl'
  return (
    <button className="flex w-full items-center gap-1 px-0 py-3" onClick={() => handleSort('used_traffic')}>
      <div className={cn('text-xs capitalize', isRTL && 'w-full md:w-auto')}>
        <span className={cn('inline-block w-full md:hidden', isRTL && 'text-end')}>{t('dataUsage')}</span>
        <span className="hidden md:block">{t('dataUsage')}</span>
      </div>
      {filters.sort && (filters.sort === 'used_traffic' || filters.sort === '-used_traffic') && (
        <ChevronDown size={16} className={`transition-transform duration-300 ${filters.sort === 'used_traffic' ? 'rotate-180' : ''} ${filters.sort === '-used_traffic' ? 'rotate-0' : ''} `} />
      )}
    </button>
  )
}
