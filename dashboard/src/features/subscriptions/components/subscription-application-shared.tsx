import { Apple, Laptop, Monitor, Smartphone, Tv } from 'lucide-react'

export function PlatformIcon({ platform }: { platform: string }) {
  switch (platform) {
    case 'android':
    case 'ios':
      return <Smartphone className="h-3.5 w-3.5" />
    case 'macos':
      return <Apple className="h-3.5 w-3.5" />
    case 'windows':
      return <Laptop className="h-3.5 w-3.5" />
    case 'linux':
      return <Monitor className="h-3.5 w-3.5" />
    case 'appletv':
    case 'androidtv':
      return <Tv className="h-3.5 w-3.5" />
    default:
      return <Monitor className="h-3.5 w-3.5" />
  }
}
