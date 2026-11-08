import { Tabs as T } from 'radix-ui'
import type { ComponentProps } from 'react'
import { cn } from '@/lib/utils'

export const Tabs = T.Root

export function TabsList({ className, ...props }: ComponentProps<typeof T.List>) {
  return <T.List className={cn('flex items-end gap-0.5 overflow-x-auto border-b border-border', className)} {...props} />
}

export function TabsTrigger({ className, ...props }: ComponentProps<typeof T.Trigger>) {
  return (
    <T.Trigger
      className={cn(
        '-mb-px h-8 shrink-0 border-b-2 border-transparent px-3 text-sm font-medium whitespace-nowrap text-muted transition-colors hover:text-fg',
        'data-[state=active]:border-accent data-[state=active]:text-fg',
        'focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-focus',
        className,
      )}
      {...props}
    />
  )
}

export function TabsContent({ className, ...props }: ComponentProps<typeof T.Content>) {
  return <T.Content className={cn('pt-3 focus-visible:outline-none', className)} {...props} />
}
