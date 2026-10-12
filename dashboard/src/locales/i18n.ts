import i18n from 'i18next'
import LanguageDetector from 'i18next-browser-languagedetector'
import HttpApi from 'i18next-http-backend'
import { initReactI18next } from 'react-i18next'
import { joinURL } from 'ufo'
import { brand } from '@/lib/brand'

// On a tenant's own domain the product name in every translation becomes the tenant's.
if (brand) {
  const { name } = brand
  i18n.use({ type: 'postProcessor', name: 'brand', process: (value: string) => value.split('Hexogate').join(name) })
}

i18n
  .use(LanguageDetector)
  .use(initReactI18next)
  .use(HttpApi)
  .init(
    {
      debug: import.meta.env.NODE_ENV === 'development',
      returnNull: false,
      fallbackLng: 'en',
      postProcess: brand ? ['brand'] : [],
      interpolation: {
        escapeValue: false,
      },
      react: {
        useSuspense: true,
      },
      load: 'languageOnly',
      detection: {
        caches: ['localStorage'],
      },
      backend: {
        loadPath: joinURL(import.meta.env.BASE_URL, `statics/locales/{{lng}}.json`),
      },
    },
    function (err) {
      if (err) {
        console.error('i18next initialization error:', err)
      }
      const lang = i18n.language
      document.documentElement.lang = lang
      document.documentElement.setAttribute('dir', i18n.dir())
    },
  )

export default i18n
