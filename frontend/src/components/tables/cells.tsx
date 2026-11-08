import { Link } from 'react-router'
import { Flag } from '../ui/data'

/** Flag + bold ticker + muted name; links to Company 360. */
export function SecurityCell({ id, ticker, name, country, link = true }: { id: string; ticker: string; name?: string; country: string; link?: boolean }) {
  const body = (
    <span className="flex min-w-0 items-center gap-1.5">
      <Flag country={country} />
      <span className="num shrink-0 font-semibold text-fg">{ticker}</span>
      {name && <span className="min-w-0 truncate text-muted">{name}</span>}
    </span>
  )
  if (!link) return body
  return (
    <Link
      to={`/company/${encodeURIComponent(id)}`}
      onClick={(e) => e.stopPropagation()}
      className="block min-w-0 rounded-sm hover:[&_.num]:text-accent-text focus-visible:outline-2 focus-visible:outline-focus"
    >
      {body}
    </Link>
  )
}
