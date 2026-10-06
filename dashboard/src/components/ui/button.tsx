import { cn } from '@/lib/utils'
import { buttonVariants } from '@/components/ui/button-variants'
import { Slot } from '@radix-ui/react-slot'
import { type VariantProps } from 'class-variance-authority'
import { LoaderCircleIcon } from 'lucide-react'
import * as React from 'react'

export interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement>, VariantProps<typeof buttonVariants> {
  asChild?: boolean
  isLoading?: boolean
  loadingText?: string
}

const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(({ className, variant, size, asChild = false, isLoading = false, loadingText, children, ...rest }, ref) => {
  const Comp = isLoading ? 'button' : asChild ? Slot : 'button'
  const content = isLoading ? (
    <>
      <LoaderCircleIcon className="h-5 w-5 animate-spin" />
      {loadingText}
    </>
  ) : (
    children
  )
  return (
    <Comp className={cn(buttonVariants({ variant, size, className }))} ref={ref} {...rest}>
      {content}
    </Comp>
  )
})
Button.displayName = 'Button'

export { Button }
