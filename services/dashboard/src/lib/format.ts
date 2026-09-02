/** Presentation helpers: dates, numbers, CSV, class names, downloads. */

/** Join conditional class names (a tiny local clsx - avoids a dependency). */
export function cx(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(' ');
}

/**
 * Parse a timestamp from hermes-core. SQLite/SQLAlchemy hands back naive
 * ISO strings like "2026-09-01T14:03:22.123456" which browsers treat as LOCAL
 * time; hermes-core writes UTC, so append a Z when no zone is present.
 */
export function parseTs(value: string | null | undefined): Date | null {
  if (!value) return null;
  const raw = String(value).trim();
  if (!raw) return null;
  const hasZone = /(?:Z|[+-]\d{2}:?\d{2})$/.test(raw);
  const normalized = hasZone ? raw : `${raw.replace(' ', 'T')}Z`;
  const date = new Date(normalized);
  return Number.isNaN(date.getTime()) ? null : date;
}

/** "Sep 1, 14:03:22" - compact absolute local time. */
export function fmtDateTime(value: string | null | undefined): string {
  const date = parseTs(value);
  if (!date) return '-';
  return date.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  });
}

/** "Sep 1, 2026" - date only. */
export function fmtDate(value: string | null | undefined): string {
  const date = parseTs(value);
  if (!date) return '-';
  return date.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
}

/** "14:03:22" - clock only, used in log gutters. */
export function fmtClock(value: string | null | undefined): string {
  const date = parseTs(value);
  if (!date) return '--:--:--';
  return date.toLocaleTimeString(undefined, {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  });
}

/** "3m ago" / "in 2h" - relative to now. */
export function fmtRelative(value: string | null | undefined): string {
  const date = parseTs(value);
  if (!date) return '-';
  const deltaMs = date.getTime() - Date.now();
  const past = deltaMs < 0;
  const seconds = Math.abs(deltaMs) / 1000;

  if (seconds < 5) return 'just now';
  // Older than ~30 days: a relative figure stops being useful.
  if (seconds >= 2592000) return fmtDate(value);

  let text: string;
  if (seconds < 60) text = `${Math.round(seconds)}s`;
  else if (seconds < 3600) text = `${Math.round(seconds / 60)}m`;
  else if (seconds < 86400) text = `${Math.round(seconds / 3600)}h`;
  else text = `${Math.round(seconds / 86400)}d`;

  return past ? `${text} ago` : `in ${text}`;
}

/** Duration between two timestamps, e.g. "1m 42s". */
export function fmtDuration(start: string | null | undefined, end: string | null | undefined): string {
  const from = parseTs(start);
  if (!from) return '-';
  const to = parseTs(end) ?? new Date();
  const seconds = Math.max(0, Math.round((to.getTime() - from.getTime()) / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  if (minutes < 60) return `${minutes}m ${rest}s`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${minutes % 60}m`;
}

/** Round to `digits`, dropping a trailing ".0". Returns "-" for null. */
export function fmtNum(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '-';
  const rounded = Number(value.toFixed(digits));
  return String(rounded);
}

/** Byte counts from Docker stats (net_rx/net_tx). */
export function fmtBytes(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '-';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let size = Math.abs(value);
  let index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  const sign = value < 0 ? '-' : '';
  return `${sign}${size < 10 && index > 0 ? size.toFixed(1) : Math.round(size)} ${units[index]}`;
}

/** Docker container ids are 64 chars; show the familiar short form. */
export function shortId(id: string | null | undefined, length = 12): string {
  if (!id) return '-';
  return id.length > length ? id.slice(0, length) : id;
}

/** Trim to a length on a word boundary, adding an ellipsis. */
export function truncate(text: string | null | undefined, max = 160): string {
  if (!text) return '';
  const clean = text.replace(/\s+/g, ' ').trim();
  if (clean.length <= max) return clean;
  const cut = clean.slice(0, max);
  const lastSpace = cut.lastIndexOf(' ');
  return `${(lastSpace > max * 0.6 ? cut.slice(0, lastSpace) : cut).trimEnd()}...`;
}

/** Strip a leading "-"/"*"/"•" bullet marker. */
export function stripBullet(line: string): string {
  return line.replace(/^\s*(?:[-*•]|\d+[.)])\s+/, '').trim();
}

/**
 * Render Docker's port mapping into "0.0.0.0:3000->80/tcp" style text.
 * The SDK's shape varies, so every branch is defensive.
 */
export function fmtPorts(ports: unknown): string {
  if (!ports) return '';
  if (typeof ports === 'string') return ports;

  if (Array.isArray(ports)) {
    return ports
      .map((entry) => {
        if (typeof entry === 'string') return entry;
        if (entry && typeof entry === 'object') {
          const e = entry as { PublicPort?: number; PrivatePort?: number; Type?: string; IP?: string };
          if (e.PrivatePort) {
            const host = e.PublicPort ? `${e.IP ?? '0.0.0.0'}:${e.PublicPort}->` : '';
            return `${host}${e.PrivatePort}/${e.Type ?? 'tcp'}`;
          }
        }
        return '';
      })
      .filter(Boolean)
      .join(', ');
  }

  if (typeof ports === 'object') {
    const out: string[] = [];
    for (const [containerPort, bindings] of Object.entries(ports as Record<string, unknown>)) {
      if (!bindings) {
        out.push(containerPort);
        continue;
      }
      const list = Array.isArray(bindings) ? bindings : [bindings];
      for (const binding of list) {
        if (binding && typeof binding === 'object') {
          const b = binding as { HostIp?: string; HostPort?: string };
          out.push(`${b.HostIp || '0.0.0.0'}:${b.HostPort ?? '?'}->${containerPort}`);
        } else if (typeof binding === 'string') {
          out.push(`${binding}->${containerPort}`);
        }
      }
    }
    return out.join(', ');
  }
  return '';
}

/* -------------------------------------------------------------------------- */
/* CSV export                                                                  */
/* -------------------------------------------------------------------------- */

export interface CsvColumn<T> {
  header: string;
  value: (row: T) => unknown;
}

function csvCell(value: unknown): string {
  if (value === null || value === undefined) return '';
  let text = typeof value === 'object' ? JSON.stringify(value) : String(value);
  // Collapse newlines so a description can't break the row structure.
  text = text.replace(/\r?\n/g, ' ').replace(/\s+/g, ' ').trim();
  if (/["',;\t]/.test(text)) {
    return `"${text.replace(/"/g, '""')}"`;
  }
  return text;
}

/** Build an RFC-4180-ish CSV document from rows + column definitions. */
export function toCsv<T>(rows: readonly T[], columns: ReadonlyArray<CsvColumn<T>>): string {
  const head = columns.map((column) => csvCell(column.header)).join(',');
  const body = rows.map((row) => columns.map((column) => csvCell(column.value(row))).join(','));
  return [head, ...body].join('\r\n');
}

/** Trigger a client-side file download (used for CSV export). */
export function downloadText(filename: string, text: string, mime = 'text/plain;charset=utf-8'): void {
  // Prepend a UTF-8 BOM for CSV so Excel detects the encoding.
  const payload = mime.startsWith('text/csv') ? `\uFEFF${text}` : text;
  const blob = new Blob([payload], { type: mime });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  anchor.rel = 'noopener';
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);
  // Give the browser a tick to start the download before revoking.
  window.setTimeout(() => URL.revokeObjectURL(url), 2000);
}

/** Timestamp suffix for exported filenames: 20260901-140322 */
export function fileStamp(date = new Date()): string {
  const pad = (n: number) => String(n).padStart(2, '0');
  return (
    `${date.getFullYear()}${pad(date.getMonth() + 1)}${pad(date.getDate())}` +
    `-${pad(date.getHours())}${pad(date.getMinutes())}${pad(date.getSeconds())}`
  );
}

/* -------------------------------------------------------------------------- */
/* Score helpers                                                              */
/* -------------------------------------------------------------------------- */

export type ScoreTone = 'good' | 'ok' | 'weak' | 'none';

/** Bucket a 0-100 score for colouring badges and gauges. */
export function scoreTone(score: number | null | undefined): ScoreTone {
  if (score === null || score === undefined || Number.isNaN(score)) return 'none';
  if (score >= 75) return 'good';
  if (score >= 50) return 'ok';
  return 'weak';
}

/** Clamp a number into a range. */
export function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}
