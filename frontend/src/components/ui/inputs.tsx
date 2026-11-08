import { Check, ChevronDown } from 'lucide-react'
import { Checkbox as RCheckbox, Slider as RSlider, Switch as RSwitch, ToggleGroup } from 'radix-ui'
import type { ComponentProps, ReactNode } from 'react'
import { cn } from '@/lib/utils'

const field =
  'h-7 rounded-md border border-border bg-surface-2 px-2 text-sm text-fg placeholder:text-faint hover:border-border-strong focus-visible:outline-2 focus-visible:outline-offset-0 focus-visible:outline-focus'

export function Input({ className, ...props }: ComponentProps<'input'>) {
  return <input className={cn(field, 'min-w-0', className)} {...props} />
}

export interface SelectOption<V extends string = string> {
  value: V
  label: string
}

/** Native select (fully keyboard/AT accessible), styled to match. */
export function Select<V extends string>({
  value,
  onChange,
  options,
  className,
  label,
  ...rest
}: { value: V; onChange: (v: V) => void; options: SelectOption<V>[]; label: string; className?: string } & Omit<ComponentProps<'select'>, 'value' | 'onChange'>) {
  return (
    <div className={cn('relative inline-flex', className)}>
      <select
        aria-label={label}
        value={value}
        onChange={(e) => onChange(e.target.value as V)}
        className={cn(field, 'w-full appearance-none pr-7')}
        {...rest}
      >
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
      <ChevronDown className="pointer-events-none absolute top-1/2 right-2 size-3.5 -translate-y-1/2 text-muted" aria-hidden />
    </div>
  )
}

/** Segmented control (single choice). */
export function Segmented<V extends string>({
  value,
  onChange,
  options,
  label,
  size = 'sm',
  className,
}: {
  value: V
  onChange: (v: V) => void
  options: { value: V; label: ReactNode; title?: string }[]
  label: string
  size?: 'xs' | 'sm'
  className?: string
}) {
  return (
    <ToggleGroup.Root
      type="single"
      value={value}
      onValueChange={(v) => v && onChange(v as V)}
      aria-label={label}
      className={cn('inline-flex rounded-md border border-border bg-surface-2 p-0.5', className)}
    >
      {options.map((o) => (
        <ToggleGroup.Item
          key={o.value}
          value={o.value}
          title={o.title}
          className={cn(
            'rounded-sm px-2 font-medium whitespace-nowrap text-muted transition-colors hover:text-fg',
            'data-[state=on]:bg-surface-3 data-[state=on]:text-fg data-[state=on]:shadow-[inset_0_-1.5px_0_var(--accent)]',
            'focus-visible:outline-2 focus-visible:outline-focus',
            size === 'xs' ? 'h-5 text-xs' : 'h-6 text-sm',
          )}
        >
          {o.label}
        </ToggleGroup.Item>
      ))}
    </ToggleGroup.Root>
  )
}

export function Slider({ label, className, ...props }: ComponentProps<typeof RSlider.Root> & { label: string }) {
  return (
    <RSlider.Root className={cn('relative flex h-4 w-full touch-none items-center select-none', className)} {...props}>
      <RSlider.Track className="relative h-1 grow overflow-hidden rounded-full bg-surface-3">
        <RSlider.Range className="absolute h-full bg-accent" />
      </RSlider.Track>
      <RSlider.Thumb
        aria-label={label}
        className="block size-3.5 rounded-full border-2 border-accent bg-surface hover:bg-surface-2 focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-focus"
      />
    </RSlider.Root>
  )
}

/** Two-thumb range slider (labels name each thumb for screen readers). */
export function RangeSlider({ labels, className, ...props }: ComponentProps<typeof RSlider.Root> & { labels: [string, string] }) {
  const thumb = 'block size-3.5 rounded-full border-2 border-accent bg-surface hover:bg-surface-2 focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-focus'
  return (
    <RSlider.Root className={cn('relative flex h-4 w-full touch-none items-center select-none', className)} minStepsBetweenThumbs={0} {...props}>
      <RSlider.Track className="relative h-1 grow overflow-hidden rounded-full bg-surface-3">
        <RSlider.Range className="absolute h-full bg-accent" />
      </RSlider.Track>
      <RSlider.Thumb aria-label={labels[0]} className={thumb} />
      <RSlider.Thumb aria-label={labels[1]} className={thumb} />
    </RSlider.Root>
  )
}

export function Switch({ label, className, ...props }: ComponentProps<typeof RSwitch.Root> & { label: string }) {
  return (
    <RSwitch.Root
      aria-label={label}
      className={cn(
        'relative inline-flex h-4 w-7 shrink-0 items-center rounded-full border border-border-strong bg-surface-3 transition-colors data-[state=checked]:border-accent data-[state=checked]:bg-accent',
        'focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-focus',
        className,
      )}
      {...props}
    >
      <RSwitch.Thumb className="block size-3 translate-x-0.5 rounded-full bg-fg transition-transform data-[state=checked]:translate-x-3 data-[state=checked]:bg-accent-fg" />
    </RSwitch.Root>
  )
}

export function Checkbox({ label, className, ...props }: ComponentProps<typeof RCheckbox.Root> & { label: string }) {
  return (
    <RCheckbox.Root
      aria-label={label}
      className={cn(
        'flex size-3.5 shrink-0 items-center justify-center rounded-sm border border-border-strong bg-surface-2 data-[state=checked]:border-accent data-[state=checked]:bg-accent',
        'focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-focus',
        className,
      )}
      {...props}
    >
      <RCheckbox.Indicator>
        <Check className="size-3 text-accent-fg" strokeWidth={3} />
      </RCheckbox.Indicator>
    </RCheckbox.Root>
  )
}
