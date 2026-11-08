import { cva, type VariantProps } from 'class-variance-authority'
import type { ComponentProps } from 'react'
import { cn } from '@/lib/utils'

export const buttonVariants = cva(
  'inline-flex items-center justify-center gap-1.5 whitespace-nowrap rounded-md font-medium transition-colors select-none disabled:pointer-events-none disabled:opacity-45 focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-focus [&_svg]:shrink-0',
  {
    variants: {
      variant: {
        primary: 'bg-accent text-accent-fg hover:bg-accent-hover font-semibold',
        secondary: 'bg-surface-2 text-fg border border-border hover:bg-surface-3 hover:border-border-strong',
        outline: 'border border-border text-fg hover:bg-surface-2 hover:border-border-strong',
        ghost: 'text-muted hover:text-fg hover:bg-surface-2',
        danger: 'border border-border text-down hover:bg-down-soft',
      },
      size: {
        xs: 'h-6 px-2 text-xs [&_svg]:size-3.5',
        sm: 'h-7 px-2.5 text-sm [&_svg]:size-3.5',
        md: 'h-8 px-3 text-base [&_svg]:size-4',
        icon: 'size-7 [&_svg]:size-4',
        'icon-sm': 'size-6 [&_svg]:size-3.5',
      },
    },
    defaultVariants: { variant: 'secondary', size: 'sm' },
  },
)

export type ButtonProps = ComponentProps<'button'> & VariantProps<typeof buttonVariants>

export function Button({ className, variant, size, type = 'button', ...props }: ButtonProps) {
  return <button type={type} className={cn(buttonVariants({ variant, size }), className)} {...props} />
}
