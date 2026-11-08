import {
  type ColumnDef,
  type ColumnPinningState,
  type OnChangeFn,
  type Row,
  type RowData,
  type SortingState,
  type VisibilityState,
  flexRender,
  getCoreRowModel,
  getSortedRowModel,
  useReactTable,
} from '@tanstack/react-table'
import { useVirtualizer } from '@tanstack/react-virtual'
import { ArrowDown, ArrowUp, ArrowUpDown } from 'lucide-react'
import { type CSSProperties, type KeyboardEvent, useRef, useState } from 'react'
import { cn } from '@/lib/utils'
import { EmptyState } from '../ui/feedback'

declare module '@tanstack/react-table' {
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  interface ColumnMeta<TData extends RowData, TValue> {
    align?: 'left' | 'right' | 'center'
    /** full label for tooltips / column chooser when the header is abbreviated */
    title?: string
    className?: string
  }
}

export interface DataTableProps<T> {
  data: T[]
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  columns: ColumnDef<T, any>[]
  ariaLabel: string
  /** fixed viewport height (virtualized). If omitted the table grows with content (no virtualization). */
  height?: number | string
  rowHeight?: number
  initialSorting?: SortingState
  sorting?: SortingState
  onSortingChange?: OnChangeFn<SortingState>
  columnPinning?: ColumnPinningState
  columnVisibility?: VisibilityState
  onColumnVisibilityChange?: OnChangeFn<VisibilityState>
  getRowId?: (row: T, index: number) => string
  onRowClick?: (row: T) => void
  rowClassName?: (row: T) => string | undefined
  emptyMessage?: string
  className?: string
  /** disable client-side sorting (data already ordered) */
  manualSorting?: boolean
}

export function DataTable<T>({
  data,
  columns,
  ariaLabel,
  height,
  rowHeight = 26,
  initialSorting = [],
  sorting: sortingProp,
  onSortingChange,
  columnPinning,
  columnVisibility,
  onColumnVisibilityChange,
  getRowId,
  onRowClick,
  rowClassName,
  emptyMessage = 'No rows match the current filters.',
  className,
  manualSorting = false,
}: DataTableProps<T>) {
  const [innerSorting, setInnerSorting] = useState<SortingState>(initialSorting)
  const sorting = sortingProp ?? innerSorting
  // TanStack Table/Virtual return non-memoisable functions by design; the React-Compiler hint is expected.
  // oxlint-disable-next-line react/incompatible-library
  const table = useReactTable({
    data,
    columns,
    state: { sorting, columnPinning: columnPinning ?? { left: [], right: [] }, ...(columnVisibility ? { columnVisibility } : {}) },
    onSortingChange: onSortingChange ?? setInnerSorting,
    onColumnVisibilityChange,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: manualSorting ? undefined : getSortedRowModel(),
    manualSorting,
    enableSortingRemoval: false,
    sortDescFirst: true,
    getRowId,
    columnResizeMode: 'onChange',
  })

  const rows = table.getRowModel().rows
  const scrollRef = useRef<HTMLDivElement>(null)
  const virtual = height !== undefined
  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => rowHeight,
    overscan: 14,
    enabled: virtual,
  })

  const items = virtual ? virtualizer.getVirtualItems() : []
  const padTop = virtual && items.length ? items[0].start : 0
  const padBottom = virtual && items.length ? virtualizer.getTotalSize() - items[items.length - 1].end : 0
  const visibleRows: Row<T>[] = virtual ? items.map((i) => rows[i.index]) : rows
  const totalWidth = table.getTotalSize()
  const colCount = table.getVisibleLeafColumns().length

  const pinStyle = (col: { getIsPinned: () => false | 'left' | 'right'; getStart: (p: 'left') => number; getIsLastColumn: (p: 'left') => boolean }): CSSProperties | undefined =>
    col.getIsPinned() === 'left' ? { position: 'sticky', left: col.getStart('left'), zIndex: 1 } : undefined

  const onKey = (e: KeyboardEvent<HTMLTableRowElement>, row: T) => {
    if (e.key === 'Enter' && onRowClick) onRowClick(row)
  }

  return (
    <div ref={scrollRef} className={cn('relative w-full overflow-auto', className)} style={{ height, maxHeight: height }}>
      <table aria-label={ariaLabel} className="border-separate border-spacing-0 text-sm" style={{ width: totalWidth, minWidth: '100%', tableLayout: 'fixed' }}>
        <colgroup>
          {table.getVisibleLeafColumns().map((c) => (
            <col key={c.id} style={{ width: c.getSize() }} />
          ))}
        </colgroup>
        <thead className="sticky top-0 z-[2]">
          {table.getHeaderGroups().map((hg) => (
            <tr key={hg.id}>
              {hg.headers.map((h) => {
                const col = h.column
                const sorted = col.getIsSorted()
                const align = col.columnDef.meta?.align ?? 'left'
                const pinned = col.getIsPinned() === 'left'
                return (
                  <th
                    key={h.id}
                    scope="col"
                    aria-sort={sorted === 'asc' ? 'ascending' : sorted === 'desc' ? 'descending' : undefined}
                    title={col.columnDef.meta?.title}
                    className={cn(
                      'h-7 border-b border-border-strong bg-surface-2 px-2 text-2xs font-semibold tracking-wide whitespace-nowrap text-muted uppercase',
                      align === 'right' && 'text-right',
                      align === 'center' && 'text-center',
                      pinned && col.getIsLastColumn('left') && 'border-r border-r-border',
                    )}
                    style={{ ...pinStyle(col), zIndex: pinned ? 3 : undefined }}
                  >
                    {h.isPlaceholder ? null : col.getCanSort() ? (
                      <button
                        type="button"
                        onClick={col.getToggleSortingHandler()}
                        className={cn('inline-flex max-w-full items-center gap-1 uppercase hover:text-fg focus-visible:outline-2 focus-visible:outline-focus', align === 'right' && 'flex-row-reverse', sorted && 'text-fg')}
                      >
                        <span className="truncate">{flexRender(col.columnDef.header, h.getContext())}</span>
                        {sorted === 'asc' ? <ArrowUp className="size-3 text-accent" aria-hidden /> : sorted === 'desc' ? <ArrowDown className="size-3 text-accent" aria-hidden /> : <ArrowUpDown className="size-3 opacity-30" aria-hidden />}
                      </button>
                    ) : (
                      <span className="truncate">{flexRender(col.columnDef.header, h.getContext())}</span>
                    )}
                  </th>
                )
              })}
            </tr>
          ))}
        </thead>
        <tbody>
          {padTop > 0 && (
            <tr aria-hidden>
              <td colSpan={colCount} style={{ height: padTop, padding: 0, border: 0 }} />
            </tr>
          )}
          {visibleRows.map((row) => {
            const extra = rowClassName?.(row.original)
            return (
              <tr
                key={row.id}
                tabIndex={onRowClick ? 0 : undefined}
                onClick={onRowClick ? () => onRowClick(row.original) : undefined}
                onKeyDown={onRowClick ? (e) => onKey(e, row.original) : undefined}
                className={cn('group', onRowClick && 'cursor-pointer focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-focus', extra)}
                style={{ height: rowHeight }}
              >
                {row.getVisibleCells().map((cell) => {
                  const col = cell.column
                  const align = col.columnDef.meta?.align ?? 'left'
                  const pinned = col.getIsPinned() === 'left'
                  return (
                    <td
                      key={cell.id}
                      className={cn(
                        'overflow-hidden border-b border-border px-2 whitespace-nowrap text-ellipsis',
                        'bg-surface group-hover:bg-surface-2',
                        align === 'right' && 'text-right',
                        align === 'center' && 'text-center',
                        pinned && col.getIsLastColumn('left') && 'border-r border-r-border',
                        col.columnDef.meta?.className,
                      )}
                      style={pinStyle(col)}
                    >
                      {flexRender(col.columnDef.cell, cell.getContext())}
                    </td>
                  )
                })}
              </tr>
            )
          })}
          {padBottom > 0 && (
            <tr aria-hidden>
              <td colSpan={colCount} style={{ height: padBottom, padding: 0, border: 0 }} />
            </tr>
          )}
        </tbody>
      </table>
      {rows.length === 0 && <EmptyState title="No results" message={emptyMessage} />}
    </div>
  )
}
