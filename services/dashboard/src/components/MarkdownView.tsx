/**
 * MarkdownView - a deliberately minimal, safe markdown renderer.
 *
 * Why not a markdown library, or `dangerouslySetInnerHTML`?
 *
 * The markdown rendered here is **LLM output**. Treating model text as trusted
 * HTML is a script-injection path: a resume containing `<img onerror=...>` or an
 * `<a href="javascript:...">` would execute in the dashboard's origin, which can
 * read the Hermes API. So this renderer never produces raw HTML at all - it
 * parses to React elements, which escape text by construction.
 *
 * Supported, because that is all an ATS-safe resume uses: headings (#..######),
 * bullet and numbered lists, bold, italic, inline code, links, horizontal rules
 * and paragraphs. Anything else renders as literal text rather than being
 * silently dropped, so the user always sees what is actually in the document.
 *
 * Link safety: only http/https/mailto survive; anything else (javascript:,
 * data:) renders as plain text. External links get noopener/noreferrer.
 */

import type { ReactNode } from 'react';

import { cx } from '../lib/format';

export interface MarkdownViewProps {
  markdown?: string | null;
  className?: string;
}

const SAFE_LINK = /^(https?:\/\/|mailto:)/i;

/** Inline formatting: bold, italic, code, links. Returns React nodes, never HTML. */
function renderInline(text: string, keyPrefix: string): ReactNode[] {
  const out: ReactNode[] = [];
  // One pass, longest-delimiter-first so ** is not eaten by *.
  const pattern = /(\[([^\]]+)\]\(([^)\s]+)\))|(\*\*([^*]+)\*\*)|(__([^_]+)__)|(\*([^*]+)\*)|(`([^`]+)`)/g;
  let last = 0;
  let match: RegExpExecArray | null;
  let i = 0;

  while ((match = pattern.exec(text)) !== null) {
    if (match.index > last) out.push(text.slice(last, match.index));
    const key = `${keyPrefix}-i${i++}`;

    if (match[1]) {
      const label = match[2];
      const href = match[3];
      if (SAFE_LINK.test(href)) {
        out.push(
          <a
            key={key}
            href={href}
            target="_blank"
            rel="noopener noreferrer"
            className="text-brand-300 underline decoration-brand-300/40 underline-offset-2 hover:text-brand-200"
          >
            {label}
          </a>,
        );
      } else {
        // Unsafe scheme: show the markdown verbatim rather than linking it.
        out.push(<span key={key}>{match[1]}</span>);
      }
    } else if (match[4] || match[6]) {
      out.push(
        <strong key={key} className="font-semibold text-ink-100">
          {match[5] ?? match[7]}
        </strong>,
      );
    } else if (match[8]) {
      out.push(
        <em key={key} className="italic">
          {match[9]}
        </em>,
      );
    } else if (match[10]) {
      out.push(
        <code key={key} className="rounded bg-ink-800 px-1 py-0.5 font-mono text-[0.95em] text-brand-200">
          {match[11]}
        </code>,
      );
    }
    last = pattern.lastIndex;
  }

  if (last < text.length) out.push(text.slice(last));
  return out;
}

const HEADING_CLASS: Record<number, string> = {
  1: 'mt-0 mb-2 text-lg font-semibold tracking-tight text-ink-100',
  2: 'mt-5 mb-1.5 border-b border-ink-700 pb-1 text-xs font-bold uppercase tracking-widest text-brand-300',
  3: 'mt-3.5 mb-1 text-sm font-semibold text-ink-100',
  4: 'mt-3 mb-1 text-xs font-semibold text-ink-200',
  5: 'mt-2 mb-1 text-xs font-medium text-ink-200',
  6: 'mt-2 mb-1 text-2xs font-medium uppercase tracking-wide text-ink-300',
};

export function MarkdownView({ markdown, className }: MarkdownViewProps) {
  const source = String(markdown ?? '').replace(/\r\n/g, '\n');

  if (!source.trim()) {
    return (
      <p className={cx('text-xs text-ink-400', className)}>
        Nothing to preview yet.
      </p>
    );
  }

  const blocks: ReactNode[] = [];
  const lines = source.split('\n');
  let paragraph: string[] = [];
  let listItems: string[] = [];
  let listOrdered = false;
  let key = 0;

  function flushParagraph() {
    if (paragraph.length === 0) return;
    const text = paragraph.join(' ');
    blocks.push(
      <p key={`p${key++}`} className="my-1.5 text-xs leading-relaxed text-ink-200">
        {renderInline(text, `p${key}`)}
      </p>,
    );
    paragraph = [];
  }

  function flushList() {
    if (listItems.length === 0) return;
    const items = listItems.map((item, idx) => (
      <li key={idx} className="text-xs leading-relaxed text-ink-200">
        {renderInline(item, `l${key}-${idx}`)}
      </li>
    ));
    blocks.push(
      listOrdered ? (
        <ol key={`ol${key++}`} className="my-1.5 list-decimal space-y-1 pl-5">
          {items}
        </ol>
      ) : (
        <ul key={`ul${key++}`} className="my-1.5 list-disc space-y-1 pl-5 marker:text-ink-500">
          {items}
        </ul>
      ),
    );
    listItems = [];
  }

  function flushAll() {
    flushParagraph();
    flushList();
  }

  for (const raw of lines) {
    const line = raw.trimEnd();

    if (!line.trim()) {
      flushAll();
      continue;
    }

    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    if (heading) {
      flushAll();
      const level = heading[1].length;
      const Tag = (`h${level}` as unknown) as 'h1';
      blocks.push(
        <Tag key={`h${key++}`} className={HEADING_CLASS[level]}>
          {renderInline(heading[2].trim(), `h${key}`)}
        </Tag>,
      );
      continue;
    }

    if (/^(-{3,}|\*{3,}|_{3,})$/.test(line.trim())) {
      flushAll();
      blocks.push(<hr key={`hr${key++}`} className="my-3 border-ink-700" />);
      continue;
    }

    const bullet = /^\s*[-*+•]\s+(.*)$/.exec(line);
    if (bullet) {
      flushParagraph();
      if (listOrdered) flushList();
      listOrdered = false;
      listItems.push(bullet[1].trim());
      continue;
    }

    const numbered = /^\s*\d+[.)]\s+(.*)$/.exec(line);
    if (numbered) {
      flushParagraph();
      if (!listOrdered) flushList();
      listOrdered = true;
      listItems.push(numbered[1].trim());
      continue;
    }

    flushList();
    paragraph.push(line.trim());
  }
  flushAll();

  return <div className={cx('max-w-none', className)}>{blocks}</div>;
}

export default MarkdownView;
