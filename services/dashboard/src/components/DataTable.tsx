/**
 * Generic sortable + filterable table.
 *
 * Deliberately unopinionated about data: a column supplies `render` for display,
 * `sortValue` for ordering and `filterValue` for the search box, so the same
 * component drives the Jobs, Runs, Resumes and Containers lists without any of
 * them leaking their row type in here.
 */

import { useEffect, useMemo, useState, type ReactNode } from 'react';

import { cx } from '../lib/format';
import { useDomId } from '../lib/hooks';
import { EmptyState } from './EmptyState';
import { ErrorState } from './ErrorState';
import { IconButton } from './Button';
import { IconChevronLeft, IconChevronRight, IconSearch } from './icons';
import { LoadingBlock } from './Spinner';

export type SortDirection = 'asc' | 'desc';
export type ColumnAlign = 'left' | 'center' | 'right';

/** A sortable/filterable cell value. `null`/`undefined`/'' always sort last. */
export type CellValue = string | number | boolean | null | undefined;

export interface DataTableColumn<T> {
  /** Stable identifier; also the fallback property name on the row object. */
  key: string;
  header: ReactNode;
  /** Cell content. Defaults to the raw value the column reads from the row. */
  render?: (row: T, index: number) => ReactNode;
  /** Ordering key. Providing this makes the column sortable. */
  sortValue?: (row: T) => CellValue;
  /** Text the search box matches against. Defaults to `sortValue` as a string. */
  filterValue?: (row: T) => string;
  align?: ColumnAlign;
  /** A CSS width, e.g. '9rem' or '1%' to shrink-wrap an action column. */
  width?: string;
  className?: string;
  headerClassName?: string;
  /** Force sortability on (uses the row's `key` property) or off. */
  sortable?: boolean;
  /** Exclude from the search box even though it has text. */
  searchable?: boolean;
}

export interface DataTableProps<T> {
  rows: readonly T[];
  columns: ReadonlyArray<DataTableColumn<T>>;
  /** Must be stable and unique - React keys and row selection both use it. */
  rowKey: (row: T, index: number) => string;

  /** First load only. A background refresh should keep the old rows visible. */
  loading?: boolean;
  /** Fetch failure; replaces the table body. */
  error?: string | null;
  onRetry?: () => void;

  searchable?: boolean;
  searchPlaceholder?: string;
  /** Page-owned filter controls, rendered left of the search box. */
  toolbar?: ReactNode;

  onRowClick?: (row: T) => void;
  isRowActive?: (row: T) => boolean;

  initialSort?: { key: string; direction: SortDirection };
  /** Rows per page. Omit or 0 to render everything. */
  pageSize?: number;

  dense?: boolean;
  stickyHeader?: boolean;

  /** Complete replacement for the empty body (Jobs.tsx supplies its own). */
  empty?: ReactNode;
  emptyTitle?: string;
  emptyDescription?: ReactNode;
  emptyAction?: ReactNode;
  /** Shown instead of emptyTitle when the search box filtered everything out. */
  noMatchTitle?: string;

  /** Visually hidden table caption - the accessible name of the table. */
  caption?: string;
  footer?: ReactNode;
  className?: string;
}

const ALIGN: Record<ColumnAlign, string> = {
  left: 'text-left',
  center: 'text-center',
  right: 'text-right',
};

function isEmptyValue(value: CellValue): boolean {
  return value === null || value === undefined || value === '';
}

function compareValues(a: CellValue, b: CellValue): number {
  if (typeof a === 'number' && typeof b === 'number') {
    if (Number.isNaN(a) && Number.isNaN(b)) return 0;
    if (Number.isNaN(a)) return 1;
    if (Number.isNaN(b)) return -1;
    return a - b;
  }
  if (typeof a === 'boolean' && typeof b === 'boolean') return Number(a) - Number(b);
  // `numeric` keeps "run 2" before "run 10", and sorts ISO timestamps correctly.
  return String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: 'base' });
}

/** Fallback reader for a column with no `sortValue`: the row's own property. */
function readProperty<T>(row: T, key: string): CellValue {
  const record = row as unknown as Record<string, unknown>;
  const value = record[key];
  if (value === null || value === undefined) return value;
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') return value;
  return null;
}

function columnSortValue<T>(column: DataTableColumn<T>, row: T): CellValue {
  return column.sortValue ? column.sortValue(row) : readProperty(row, column.key);
}

function columnSearchText<T>(column: DataTableColumn<T>, row: T): string {
  if (column.filterValue) return column.filterValue(row);
  const value = columnSortValue(column, row);
  return isEmptyValue(value) ? '' : String(value);
}

function isSortable<T>(column: DataTableColumn<T>): boolean {
  if (column.sortable !== undefined) return column.sortable;
  return Boolean(column.sortValue);
}

export function DataTable<T>({
  rows,
  columns,
  rowKey,
  loading = false,
  error = null,
  onRetry,
  searchable = false,
  searchPlaceholder = 'Filter...',
  toolbar,
  onRowClick,
  isRowActive,
  initialSort,
  pageSize = 0,
  dense = false,
  stickyHeader = false,
  empty,
  emptyTitle = 'Nothing here yet',
  emptyDescription,
  emptyAction,
  noMatchTitle = 'No rows match your filter',
  caption,
  footer,
  className,
}: DataTableProps<T>) {
  const [query, setQuery] = useState('');
  const [sort, setSort] = useState<{ key: string; direction: SortDirection } | null>(initialSort ?? null);
  const [page, setPage] = useState(0);
  const searchId = useDomId('table-search');

  const visibleColumns = useMemo(() => columns.filter((column) => column.key), [columns]);

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return rows.slice();
    const searchCols = visibleColumns.filter((column) => column.searchable !== false);
    return rows.filter((row) =>
      searchCols.some((column) => columnSearchText(column, row).toLowerCase().includes(needle)),
    );
  }, [rows, query, visibleColumns]);

  const sorted = useMemo(() => {
    if (!sort) return filtered;
    const column = visibleColumns.find((candidate) => candidate.key === sort.key);
    if (!column) return filtered;
    const sign = sort.direction === 'asc' ? 1 : -1;

    return filtered.slice().sort((left, right) => {
      const a = columnSortValue(column, left);
      const b = columnSortValue(column, right);
      const aEmpty = isEmptyValue(a);
      const bEmpty = isEmptyValue(b);
      // Blanks sink to the bottom in both directions - a column of "-" at the
      // top of a descending sort is never what the user wanted.
      if (aEmpty && bEmpty) return 0;
      if (aEmpty) return 1;
      if (bEmpty) return -1;
      return compareValues(a, b) * sign;
    });
  }, [filtered, sort, visibleColumns]);

  const perPage = pageSize > 0 ? pageSize : 0;
  const pageCount = perPage ? Math.max(1, Math.ceil(sorted.length / perPage)) : 1;

  // Clamp the page whenever filtering/sorting shrinks the result set.
  useEffect(() => {
    setPage((current) => Math.min(current, pageCount - 1));
  }, [pageCount]);

  useEffect(() => {
    setPage(0);
  }, [query, sort?.key, sort?.direction]);

  const pageRows = perPage ? sorted.slice(page * perPage, page * perPage + perPage) : sorted;

  const toggleSort = (column: DataTableColumn<T>) => {
    if (!isSortable(column)) return;
    setSort((current) => {
      if (!current || current.key !== column.key) return { key: column.key, direction: 'asc' };
      if (current.direction === 'asc') return { key: column.key, direction: 'desc' };
      // Third click clears the sort and restores the server's order.
      return null;
    });
  };

  const cellPad = dense ? 'px-3 py-1.5' : 'px-3 py-2.5';
  const showToolbar = searchable || Boolean(toolbar);

  const body = (() => {
    if (error) {
      return (
        <ErrorState
          message={error}
          title="Could not load this list"
          onRetry={onRetry}
          className="m-4"
        />
      );
    }
    if (loading && !rows.length) return <LoadingBlock />;
    if (!rows.length) {
      // A page-supplied `empty` wins: Jobs.tsx needs to explain *why* the list is
      // empty (no LinkedIn session vs. no search run yet), which a generic
      // title cannot express.
      if (empty) return <>{empty}</>;
      return <EmptyState title={emptyTitle} description={emptyDescription} action={emptyAction} />;
    }
    if (!sorted.length) {
      return (
        <EmptyState
          compact
          title={noMatchTitle}
          description={`No row matches "${query.trim()}".`}
          icon={<IconSearch size={18} />}
        />
      );
    }
    return null;
  })();

  return (
    <div className={cx('flex min-w-0 flex-col', className)}>
      {showToolbar ? (
        <div className="flex flex-wrap items-center gap-2 border-b border-ink-750 px-3 py-2.5">
          {toolbar}
          {searchable ? (
            <div className="relative ml-auto min-w-[10rem] flex-1 sm:max-w-xs">
              <label htmlFor={searchId} className="sr-only">
                Filter rows
              </label>
              <IconSearch
                size={14}
                className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-ink-400"
              />
              <input
                id={searchId}
                type="search"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder={searchPlaceholder}
                autoComplete="off"
                spellCheck={false}
                className="field h-8 py-1 pl-8 text-xs"
              />
            </div>
          ) : null}
        </div>
      ) : null}

      {body ?? (
        <div className="min-w-0 overflow-x-auto">
          <table className="w-full min-w-full border-collapse text-sm">
            {caption ? <caption className="sr-only">{caption}</caption> : null}
            <thead className={cx(stickyHeader && 'sticky top-0 z-10')}>
              <tr className="border-b border-ink-750 bg-ink-850">
                {visibleColumns.map((column) => {
                  const sortable = isSortable(column);
                  const active = sort?.key === column.key;
                  const ariaSort = active ? (sort?.direction === 'asc' ? 'ascending' : 'descending') : 'none';
                  return (
                    <th
                      key={column.key}
                      scope="col"
                      aria-sort={sortable ? ariaSort : undefined}
                      style={column.width ? { width: column.width } : undefined}
                      className={cx(
                        'whitespace-nowrap px-3 py-2 text-2xs font-semibold uppercase tracking-wide text-ink-300',
                        ALIGN[column.align ?? 'left'],
                        column.headerClassName,
                      )}
                    >
                      {sortable ? (
                        <button
                          type="button"
                          onClick={() => toggleSort(column)}
                          className={cx(
                            'focus-ring -mx-1 inline-flex max-w-full items-center gap-1 rounded px-1 py-0.5 uppercase tracking-wide ring-offset-ink-850 transition-colors hover:text-ink-100',
                            active && 'text-brand-300',
                          )}
                          title={
                            active
                              ? sort?.direction === 'asc'
                                ? 'Sorted ascending - click for descending'
                                : 'Sorted descending - click to clear'
                              : 'Click to sort ascending'
                          }
                        >
                          <span className="truncate">{column.header}</span>
                          <SortCaret direction={active ? sort?.direction : undefined} />
                        </button>
                      ) : (
                        column.header
                      )}
                    </th>
                  );
                })}
              </tr>
            </thead>
            <tbody>
              {pageRows.map((row, index) => {
                const key = rowKey(row, index);
                const active = isRowActive?.(row) ?? false;
                const clickable = Boolean(onRowClick);
                return (
                  <tr
                    key={key}
                    onClick={clickable ? () => onRowClick?.(row) : undefined}
                    onKeyDown={
                      clickable
                        ? (event) => {
                            if (event.key === 'Enter' || event.key === ' ') {
                              event.preventDefault();
                              onRowClick?.(row);
                            }
                          }
                        : undefined
                    }
                    tabIndex={clickable ? 0 : undefined}
                    role={clickable ? 'button' : undefined}
                    aria-current={active ? 'true' : undefined}
                    className={cx(
                      'border-b border-ink-800/70 transition-colors last:border-b-0',
                      clickable && 'focus-ring cursor-pointer hover:bg-ink-850 ring-offset-ink-875',
                      active && 'bg-brand-800/25',
                    )}
                  >
                    {visibleColumns.map((column) => (
                      <td
                        key={column.key}
                        className={cx(
                          'align-middle text-ink-200',
                          cellPad,
                          ALIGN[column.align ?? 'left'],
                          column.className,
                        )}
                      >
                        {column.render
                          ? column.render(row, index)
                          : renderFallback(columnSortValue(column, row))}
                      </td>
                    ))}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {(perPage && sorted.length > perPage) || footer ? (
        <div className="flex flex-wrap items-center justify-between gap-3 border-t border-ink-750 px-3 py-2 text-xs text-ink-400">
          <div className="min-w-0">{footer}</div>
          {perPage && sorted.length > perPage ? (
            <div className="flex items-center gap-2">
              <span className="nums">
                {page * perPage + 1}-{Math.min(sorted.length, (page + 1) * perPage)} of {sorted.length}
              </span>
              <IconButton
                label="Previous page"
                icon={<IconChevronLeft size={14} />}
                variant="subtle"
                size="xs"
                disabled={page === 0}
                onClick={() => setPage((current) => Math.max(0, current - 1))}
              />
              <IconButton
                label="Next page"
                icon={<IconChevronRight size={14} />}
                variant="subtle"
                size="xs"
                disabled={page >= pageCount - 1}
                onClick={() => setPage((current) => Math.min(pageCount - 1, current + 1))}
              />
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

/** Plain text for a column with no `render`. */
function renderFallback(value: CellValue): ReactNode {
  if (isEmptyValue(value)) return <span className="text-ink-500">-</span>;
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  return String(value);
}

/** Two stacked triangles; the active direction is opaque, the other is faint. */
function SortCaret({ direction }: { direction?: SortDirection }) {
  return (
    <svg width="8" height="12" viewBox="0 0 8 12" aria-hidden="true" className="shrink-0" focusable="false">
      <path
        d="M4 1 7 5H1L4 1Z"
        fill="currentColor"
        opacity={direction === 'asc' ? 1 : direction ? 0.2 : 0.35}
      />
      <path
        d="M4 11 1 7h6l-3 4Z"
        fill="currentColor"
        opacity={direction === 'desc' ? 1 : direction ? 0.2 : 0.35}
      />
    </svg>
  );
}
