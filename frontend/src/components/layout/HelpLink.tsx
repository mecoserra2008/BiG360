import { CircleHelp } from 'lucide-react'
import { Link, useLocation } from 'react-router'
import { helpHref } from '@/lib/guide/helpMap'
import { cn } from '@/lib/utils'
import { Tip } from '../ui/tooltip'

/** "?" button of a page header: opens the user-guide section of the current page (route -> anchor map). */
export function HelpLink({ className }: { className?: string }) {
  const { pathname, search } = useLocation()
  if (pathname.startsWith('/help')) return null
  return (
    <Tip content="Guide: how this page works">
      <Link
        to={helpHref(pathname, search)}
        aria-label="Open the user guide for this page"
        className={cn('inline-flex size-6 shrink-0 items-center justify-center rounded-md text-faint hover:bg-surface-2 hover:text-fg focus-visible:outline-2 focus-visible:outline-focus print:hidden', className)}
      >
        <CircleHelp className="size-4" aria-hidden />
      </Link>
    </Tip>
  )
}
