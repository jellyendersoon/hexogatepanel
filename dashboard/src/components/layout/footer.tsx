import { FC } from 'react'
import { useTranslation } from 'react-i18next'

const FooterContent = () => {
  const { t } = useTranslation()
  return <p className="text-muted-foreground inline-block flex-grow text-center text-xs">{t('hexogate')}</p>
}

export const Footer: FC = ({ ...props }) => {
  return (
    <div className="relative flex w-full pt-1 pb-3" {...props}>
      <FooterContent />
    </div>
  )
}
