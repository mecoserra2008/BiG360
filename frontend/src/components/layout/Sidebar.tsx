import { PanelLeftClose, PanelLeftOpen } from 'lucide-react'
import { NavLink, useLocation } from 'react-router'
import logoOrange from '@/assets/big-logo-orange@sm.png'
import { cn } from '@/lib/utils'
import { useUi } from '@/store'
import { Tip } from '../ui/tooltip'
import { NAV } from './nav'

export function Sidebar({ lastCompanyId }: { lastCompanyId: string | null }) {
  const collapsed = useUi((s) => s.navCollapsed)
  const toggle = useUi((s) => s.toggleNav)
  const { pathname } = useLocation()

  return (
    <nav
      aria-label="Primary"
      className={cn('flex h-full shrink-0 flex-col border-r border-border bg-surface transition-[width] duration-150', collapsed ? 'w-[52px]' : 'w-[200px]')}
    >
      <div className={cn('flex h-11 items-center border-b border-border', collapsed ? 'justify-center px-1' : 'gap-2 px-3')}>
        <img src={logoOrange} alt="BiG — Banco de Investimento Global" className={cn('w-auto select-none', collapsed ? 'h-[18px]' : 'h-[20px]')} draggable={false} />
        {!collapsed && (
          <div className="min-w-0 leading-none">
            <div className="text-sm font-semibold tracking-tight text-fg">Equity 360</div>
            <div className="text-2xs tracking-wide text-faint uppercase">Research terminal</div>
          </div>
        )}
      </div>
      <ul className="flex-1 space-y-0.5 overflow-y-auto p-1.5">
        {NAV.map((item) => {
          const to = item.match === '/company' && lastCompanyId ? `/company/${encodeURIComponent(lastCompanyId)}` : item.to
          const active = pathname.startsWith(item.match)
          const link = (
            <NavLink
              to={to}
              aria-current={active ? 'page' : undefined}
              aria-label={collapsed ? item.label : undefined}
              className={cn(
                'relative flex h-8 items-center gap-2.5 rounded-md text-sm font-medium transition-colors focus-visible:outline-2 focus-visible:outline-focus',
                collapsed ? 'justify-center' : 'px-2.5',
                active ? 'bg-surface-3 text-fg' : 'text-muted hover:bg-surface-2 hover:text-fg',
              )}
            >
              {active && <span className="absolute top-1.5 bottom-1.5 left-0 w-[2px] rounded-full bg-accent" aria-hidden />}
              <item.icon className={cn('size-4 shrink-0', active && 'text-accent')} aria-hidden />
              {!collapsed && <span className="truncate">{item.label}</span>}
            </NavLink>
          )
          return (
            <li key={item.to}>
              {collapsed ? (
                <Tip content={item.label} side="right">
                  {link}
                </Tip>
              ) : (
                link
              )}
              {active && !collapsed && item.children && (
                <ul className="mt-0.5 mb-1 ml-[22px] space-y-px border-l border-border pl-2" aria-label={`${item.label} pages`}>
                  {item.children.map((c) => {
                    const on = c.exact ? pathname === c.match || pathname === `${c.match}/` : pathname.startsWith(c.match)
                    return (
                      <li key={c.to}>
                        <NavLink
                          to={c.to}
                          aria-current={on ? 'page' : undefined}
                          className={cn('block h-6 truncate rounded-sm px-2 text-xs leading-6 transition-colors', on ? 'bg-surface-2 font-medium text-fg' : 'text-muted hover:text-fg')}
                        >
                          {c.label}
                        </NavLink>
                      </li>
                    )
                  })}
                </ul>
              )}
            </li>
          )
        })}
      </ul>
      <div className="border-t border-border p-1.5">
        <button
          type="button"
          onClick={toggle}
          aria-label={collapsed ? 'Expand navigation' : 'Collapse navigation'}
          className={cn('flex h-8 w-full items-center gap-2.5 rounded-md text-sm text-muted hover:bg-surface-2 hover:text-fg focus-visible:outline-2 focus-visible:outline-focus', collapsed ? 'justify-center' : 'px-2.5')}
        >
          {collapsed ? <PanelLeftOpen className="size-4" aria-hidden /> : <PanelLeftClose className="size-4" aria-hidden />}
          {!collapsed && <span>Collapse</span>}
        </button>
      </div>
    </nav>
  )
}
