import { CircleAlert, CircleCheck, Info, X } from 'lucide-react'
import { create } from 'zustand'
import { cn } from '@/lib/utils'

export type ToastTone = 'success' | 'error' | 'info'
interface Toast {
  id: number
  tone: ToastTone
  title: string
  message?: string
}

interface ToastState {
  toasts: Toast[]
  push: (t: Omit<Toast, 'id'>) => void
  dismiss: (id: number) => void
}

let seq = 0
export const useToasts = create<ToastState>()((set, get) => ({
  toasts: [],
  push: (t) => {
    const id = ++seq
    set({ toasts: [...get().toasts.slice(-3), { ...t, id }] })
    setTimeout(() => get().dismiss(id), t.tone === 'error' ? 9000 : 5000)
  },
  dismiss: (id) => set({ toasts: get().toasts.filter((x) => x.id !== id) }),
}))

export const toast = (t: Omit<Toast, 'id'>): void => useToasts.getState().push(t)

const ICON = { success: CircleCheck, error: CircleAlert, info: Info }

/** Bottom-right toast stack (polite live region; errors are assertive). */
export function Toaster() {
  const toasts = useToasts((s) => s.toasts)
  const dismiss = useToasts((s) => s.dismiss)
  return (
    <div className="pointer-events-none fixed right-4 bottom-4 z-50 flex w-[340px] flex-col gap-2" aria-live="polite">
      {toasts.map((t) => {
        const Icon = ICON[t.tone]
        return (
          <div
            key={t.id}
            role={t.tone === 'error' ? 'alert' : 'status'}
            className={cn(
              'pointer-events-auto flex gap-2 rounded-md border bg-surface px-3 py-2 text-sm',
              t.tone === 'success' ? 'border-up/50' : t.tone === 'error' ? 'border-down/50' : 'border-border-strong',
            )}
          >
            <Icon className={cn('mt-0.5 size-4 shrink-0', t.tone === 'success' ? 'text-up' : t.tone === 'error' ? 'text-down' : 'text-info')} aria-hidden />
            <div className="min-w-0 flex-1">
              <div className="font-medium text-fg">{t.title}</div>
              {t.message && <div className="mt-0.5 text-xs break-words text-muted">{t.message}</div>}
            </div>
            <button type="button" aria-label="Dismiss notification" onClick={() => dismiss(t.id)} className="text-faint hover:text-fg">
              <X className="size-3.5" />
            </button>
          </div>
        )
      })}
    </div>
  )
}
