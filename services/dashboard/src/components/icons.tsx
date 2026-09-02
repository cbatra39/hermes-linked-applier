/**
 * Inline SVG icon set.
 *
 * Hand-drawn rather than pulled from an icon package on purpose: package.json is
 * frozen (see the build brief), and Hermes must build with zero network access.
 * Every icon is a 24x24 stroked path that inherits `currentColor`, so it picks up
 * the colour of whatever button/badge it sits in.
 *
 * All icons are `aria-hidden` by default - they are decorative next to a text
 * label. Pass `aria-hidden={false}` plus `aria-label="..."` (or `title`) for the
 * rare icon-only control that carries the whole meaning.
 */

import type { ReactNode, SVGProps } from 'react';

export interface IconProps extends Omit<SVGProps<SVGSVGElement>, 'width' | 'height' | 'viewBox'> {
  /** Rendered square size in px. Default 16 - matches a 14px text line. */
  size?: number;
}

function Icon({ size = 16, children, ...rest }: IconProps & { children: ReactNode }) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.75}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
      shapeRendering="geometricPrecision"
      {...rest}
    >
      {children}
    </svg>
  );
}

/* ------------------------------------------------------------------ branding */

/** The Hermes "H" mark - same geometry as the favicon in index.html. */
export function HermesMark({ size = 28, className }: { size?: number; className?: string }) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      width={size}
      height={size}
      viewBox="0 0 32 32"
      className={className}
      role="img"
      aria-label="Hermes"
    >
      <rect width="32" height="32" rx="7" fill="currentColor" opacity="0.14" />
      <rect x="0.6" y="0.6" width="30.8" height="30.8" rx="6.6" fill="none" stroke="currentColor" strokeOpacity="0.35" strokeWidth="1.2" />
      <path d="M9 23V9h2.6v5.6h8.8V9H23v14h-2.6v-5.9h-8.8V23H9z" fill="currentColor" />
    </svg>
  );
}

/* ----------------------------------------------------------------- navigation */

export function IconOverview(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M3 10.4 12 3l9 7.4" />
      <path d="M5.4 9.4V20a1 1 0 0 0 1 1h11.2a1 1 0 0 0 1-1V9.4" />
      <path d="M9.6 21v-6.2h4.8V21" />
    </Icon>
  );
}

export function IconContainers(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M12 2.8 3.2 7.4v9.2L12 21.2l8.8-4.6V7.4L12 2.8Z" />
      <path d="M3.2 7.4 12 12l8.8-4.6" />
      <path d="M12 12v9.2" />
    </Icon>
  );
}

export function IconLinkedIn(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M10.1 13.3a4.6 4.6 0 0 0 6.9.5l2.7-2.7a4.6 4.6 0 0 0-6.5-6.5l-1.6 1.5" />
      <path d="M13.9 10.7a4.6 4.6 0 0 0-6.9-.5l-2.7 2.7a4.6 4.6 0 0 0 6.5 6.5l1.6-1.5" />
    </Icon>
  );
}

export function IconResume(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M14.2 2.8H6.8a1.8 1.8 0 0 0-1.8 1.8v14.8a1.8 1.8 0 0 0 1.8 1.8h10.4a1.8 1.8 0 0 0 1.8-1.8V7.6l-4.8-4.8Z" />
      <path d="M14 2.9V8h5" />
      <path d="M8.4 12.6h7.2" />
      <path d="M8.4 16.2h7.2" />
      <path d="M8.4 9h2.6" />
    </Icon>
  );
}

export function IconJobs(props: IconProps) {
  return (
    <Icon {...props}>
      <rect x="2.8" y="7.2" width="18.4" height="13" rx="2" />
      <path d="M15.6 20.2V5.8a1.8 1.8 0 0 0-1.8-1.8h-3.6a1.8 1.8 0 0 0-1.8 1.8v14.4" />
      <path d="M2.8 12.4h18.4" />
    </Icon>
  );
}

export function IconRuns(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M2.6 12.4h3.9l2.4-6.8 4.6 13 2.4-6.2h3.5" />
    </Icon>
  );
}

export function IconSettings(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M3 6.4h4.2" />
      <path d="M11.6 6.4H21" />
      <path d="M3 12h9.4" />
      <path d="M16.8 12H21" />
      <path d="M3 17.6h4.2" />
      <path d="M11.6 17.6H21" />
      <circle cx="9.4" cy="6.4" r="2.2" />
      <circle cx="14.6" cy="12" r="2.2" />
      <circle cx="9.4" cy="17.6" r="2.2" />
    </Icon>
  );
}

/* ---------------------------------------------------------------------- chrome */

export function IconMenu(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M3.5 6.5h17" />
      <path d="M3.5 12h17" />
      <path d="M3.5 17.5h17" />
    </Icon>
  );
}

export function IconClose(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M18 6 6 18" />
      <path d="M6 6l12 12" />
    </Icon>
  );
}

export function IconChevronDown(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="m6 9.2 6 6 6-6" />
    </Icon>
  );
}

export function IconChevronUp(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="m18 14.8-6-6-6 6" />
    </Icon>
  );
}

export function IconChevronLeft(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="m14.8 6-6 6 6 6" />
    </Icon>
  );
}

export function IconChevronRight(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="m9.2 6 6 6-6 6" />
    </Icon>
  );
}

export function IconSearch(props: IconProps) {
  return (
    <Icon {...props}>
      <circle cx="10.8" cy="10.8" r="6.8" />
      <path d="m20 20-4.4-4.4" />
    </Icon>
  );
}

export function IconRefresh(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M20.4 12a8.4 8.4 0 1 1-2.5-5.95" />
      <path d="M20.4 3.6v5.2h-5.2" />
    </Icon>
  );
}

export function IconPlay(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M8.4 5.3 18.6 12 8.4 18.7V5.3Z" />
    </Icon>
  );
}

export function IconPause(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M9.4 5v14" />
      <path d="M14.6 5v14" />
    </Icon>
  );
}

export function IconStop(props: IconProps) {
  return (
    <Icon {...props}>
      <rect x="6.2" y="6.2" width="11.6" height="11.6" rx="1.6" />
    </Icon>
  );
}

export function IconTrash(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M3.8 6.8h16.4" />
      <path d="M9.6 4.4h4.8" />
      <path d="M6.4 6.8l.9 12.6a1.6 1.6 0 0 0 1.6 1.5h6.2a1.6 1.6 0 0 0 1.6-1.5l.9-12.6" />
      <path d="M10.4 10.8v6" />
      <path d="M13.6 10.8v6" />
    </Icon>
  );
}

export function IconDownload(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M12 3.4v11.4" />
      <path d="m7.4 10.4 4.6 4.6 4.6-4.6" />
      <path d="M4 19.4h16" />
    </Icon>
  );
}

export function IconUpload(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M12 15V3.6" />
      <path d="m7.4 8.2 4.6-4.6 4.6 4.6" />
      <path d="M4 19.4h16" />
    </Icon>
  );
}

export function IconExternal(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M14.2 4h5.8v5.8" />
      <path d="M20 4l-8.6 8.6" />
      <path d="M17.6 14v5a1.4 1.4 0 0 1-1.4 1.4H5a1.4 1.4 0 0 1-1.4-1.4V7.8A1.4 1.4 0 0 1 5 6.4h5" />
    </Icon>
  );
}

export function IconWarning(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M10.3 4.2 2.1 18.4a2 2 0 0 0 1.7 3h16.4a2 2 0 0 0 1.7-3L13.7 4.2a2 2 0 0 0-3.4 0Z" />
      <path d="M12 9.6v4.6" />
      <path d="M12 17.6h.01" />
    </Icon>
  );
}

export function IconCheckCircle(props: IconProps) {
  return (
    <Icon {...props}>
      <circle cx="12" cy="12" r="8.8" />
      <path d="m8.2 12.3 2.5 2.5 5.1-5.6" />
    </Icon>
  );
}

export function IconCheck(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="m5 12.8 4.4 4.4L19 7.4" />
    </Icon>
  );
}

export function IconInfo(props: IconProps) {
  return (
    <Icon {...props}>
      <circle cx="12" cy="12" r="8.8" />
      <path d="M12 11.2v5.2" />
      <path d="M12 7.9h.01" />
    </Icon>
  );
}

export function IconInbox(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M3.6 13.2h4.2l1.5 2.8h5.4l1.5-2.8h4.2" />
      <path d="M3.6 13.2 6 5.9a2 2 0 0 1 1.9-1.4h8.2A2 2 0 0 1 18 5.9l2.4 7.3v4.9a2 2 0 0 1-2 2H5.6a2 2 0 0 1-2-2v-4.9Z" />
    </Icon>
  );
}

export function IconCopy(props: IconProps) {
  return (
    <Icon {...props}>
      <rect x="9" y="9" width="11.4" height="11.4" rx="2" />
      <path d="M5.6 15H5a1.4 1.4 0 0 1-1.4-1.4V5A1.4 1.4 0 0 1 5 3.6h8.6A1.4 1.4 0 0 1 15 5v.6" />
    </Icon>
  );
}

export function IconTerminal(props: IconProps) {
  return (
    <Icon {...props}>
      <rect x="2.8" y="4" width="18.4" height="16" rx="2" />
      <path d="m6.8 9.6 2.8 2.6-2.8 2.6" />
      <path d="M12.8 15h4.4" />
    </Icon>
  );
}

export function IconTarget(props: IconProps) {
  return (
    <Icon {...props}>
      <circle cx="12" cy="12" r="8.8" />
      <circle cx="12" cy="12" r="4.8" />
      <circle cx="12" cy="12" r="1" fill="currentColor" stroke="none" />
    </Icon>
  );
}

export function IconSparkle(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M12 3.2l1.7 4.9 4.9 1.7-4.9 1.7L12 16.4l-1.7-4.9-4.9-1.7 4.9-1.7L12 3.2Z" />
      <path d="M18.6 16.4l.7 2 2 .7-2 .7-.7 2-.7-2-2-.7 2-.7.7-2Z" />
    </Icon>
  );
}

export function IconFilter(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M3.6 5.4h16.8l-6.4 7.6v6l-4-2.2v-3.8L3.6 5.4Z" />
    </Icon>
  );
}
