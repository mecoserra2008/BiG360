import { Compass, TriangleAlert } from 'lucide-react'
import { Link, useRouteError } from 'react-router'
import { buttonVariants } from '@/components/ui/button'
import { EmptyState } from '@/components/ui/feedback'

export function NotFound({ crashed = false }: { crashed?: boolean }) {
  return crashed ? <Crashed /> : (
    <EmptyState
      icon={<Compass />}
      title="Page not found"
      message="The address does not match any view in BiG Equity 360."
      action={<Link to="/macro" className={buttonVariants({ variant: 'outline', size: 'sm' })}>Back to Macro</Link>}
    />
  )
}

function Crashed() {
  const err = useRouteError()
  const msg = err instanceof Error ? err.message : String(err ?? 'Unknown error')
  return (
    <div className="flex h-full items-center justify-center bg-bg p-6 text-fg">
      <EmptyState
        icon={<TriangleAlert className="text-down" />}
        title="Something went wrong"
        message={msg}
        action={<a href="/macro" className={buttonVariants({ variant: 'outline', size: 'sm' })}>Reload</a>}
      />
    </div>
  )
}
