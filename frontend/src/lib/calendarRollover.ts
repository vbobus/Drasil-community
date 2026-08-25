import type { CalendarBlock } from '../types/calendar';

export const READY_TO_REVISIT = 'ready-to-revisit' as const;

export type CalendarRolloverState = typeof READY_TO_REVISIT | null;

function isCompletedStatus(status?: string | null) {
  return status === 'done' || status === 'completed';
}

export function calendarRolloverState(
  block: CalendarBlock,
  referenceTime: Date = new Date(),
  resolvedTaskStatus: string | null | undefined = block.taskStatus,
): CalendarRolloverState {
  if (block.variant === 'draft' || block.variant === 'proposed') {
    return null;
  }
  if (
    isCompletedStatus(resolvedTaskStatus)
    || block.completionStatus === 'completed'
  ) {
    return null;
  }

  const endAt = Date.parse(block.endAt);
  if (Number.isNaN(endAt) || endAt > referenceTime.getTime()) {
    return null;
  }

  return READY_TO_REVISIT;
}
