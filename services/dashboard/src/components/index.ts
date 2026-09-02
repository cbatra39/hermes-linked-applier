/**
 * Barrel for the shared UI kit.
 *
 * Pages import everything from '../components' rather than reaching into
 * individual files, so a component can be split or renamed without touching
 * seven page files. Keep this list exhaustive — a component missing here is a
 * build error in whichever page uses it, not a runtime surprise.
 */

export { Badge, ScoreBadge } from './Badge';
export type { BadgeTone, BadgeProps } from './Badge';
export {
  containerStateTone,
  jobStatusTone,
  runStatusTone,
  scoreBadgeTone,
  verdictTone,
} from './Badge';

export { Button, AnchorButton, IconButton, buttonClasses } from './Button';
export type {
  ButtonProps,
  ButtonSize,
  ButtonVariant,
  AnchorButtonProps,
  IconButtonProps,
} from './Button';

export { Card, PageHeader, Stat, FieldList } from './Card';
export type { CardProps, PageHeaderProps, StatProps, FieldRow } from './Card';

export { ConfirmDialog } from './ConfirmDialog';
export type { ConfirmDialogProps } from './ConfirmDialog';

export { DataTable } from './DataTable';
export type {
  DataTableColumn,
  DataTableProps,
  SortDirection,
  ColumnAlign,
  CellValue,
} from './DataTable';

export { Drawer } from './Drawer';
export type { DrawerProps, DrawerSide, DrawerSize } from './Drawer';

export { EmptyState } from './EmptyState';
export type { EmptyStateProps } from './EmptyState';

export { ErrorState, WarningNote } from './ErrorState';
export type { ErrorStateProps } from './ErrorState';

export { KeywordChips } from './KeywordChips';
export type { KeywordChipsProps, KeywordTone } from './KeywordChips';

export { LogStream } from './LogStream';
export type { LogStreamProps } from './LogStream';

export { MarkdownView } from './MarkdownView';
export type { MarkdownViewProps } from './MarkdownView';

export { Modal } from './Modal';
export type { ModalProps, ModalSize } from './Modal';

export { ScoreGauge } from './ScoreGauge';
export type { ScoreGaugeProps, ScoreGaugeSize } from './ScoreGauge';

export { Spinner, LoadingBlock, SkeletonBar } from './Spinner';
export type { SpinnerProps } from './Spinner';

export { StatusDot } from './StatusDot';
export type { StatusDotProps, StatusTone } from './StatusDot';

export { StatusSelect } from './StatusSelect';
export type { StatusSelectProps } from './StatusSelect';

export { SubscoreBars } from './SubscoreBars';
export type { SubscoreBarsProps } from './SubscoreBars';

export * from './icons';
export { useBodyScrollLock, useEscapeKey, useFocusTrap, FOCUSABLE_SELECTOR } from './overlay';
