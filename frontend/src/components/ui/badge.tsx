import { cva, type VariantProps } from 'class-variance-authority'
import type { ComponentProps } from 'react'
import { cn } from '@/lib/utils'

export const badgeVariants = cva(
  'inline-flex items-center gap-1 rounded-sm border px-1.5 h-5 text-2xs font-semibold uppercase tracking-wide whitespace-nowrap [&_svg]:size-3',
  {
    variants: {
      tone: {
        neutral: 'border-border bg-surface-2 text-muted',
        accent: 'border-transparent bg-accent-soft text-accent-text',
        up: 'border-transparent bg-up-soft text-up',
        down: 'border-transparent bg-down-soft text-down',
        warn: 'border-transparent bg-warn-soft text-warn',
        info: 'border-transparent bg-info-soft text-info',
        solid: 'border-transparent bg-accent text-accent-fg',
      },
    },
    defaultVariants: { tone: 'neutral' },
  },
)

export type BadgeProps = ComponentProps<'span'> & VariantProps<typeof badgeVariants>

export function Badge({ className, tone, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ tone }), className)} {...props} />
}
