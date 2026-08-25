export type CalendarViewMode = 'day' | 'week' | 'month';
export type CalendarBlockVariant = 'scheduled' | 'protected' | 'draft' | 'proposed';
export type CalendarCompletionStatus =
  | 'not_started'
  | 'in_progress'
  | 'completed'
  | 'skipped'
  | 'missed'
  | 'partial';

export interface CalendarBlockChild {
  id: string;
  title: string;
  emoji?: string | null;
  status?: CalendarCompletionStatus | 'todo' | 'in-progress' | 'done';
  timeEstimateMinutes?: number | null;
  children?: CalendarBlockChild[];
}

export interface CalendarBlock {
  id: string;
  taskId: string;
  taskStatus?: CalendarCompletionStatus | 'todo' | 'in-progress' | 'done';
  taskDeadlineAt?: string | null;
  taskPriority?: 'low' | 'medium' | 'high';
  timeEstimateMinutes?: number | null;
  categoryId: number;
  title: string;
  emoji?: string | null;
  categoryName: string;
  categoryColor: string;
  startAt: string;
  endAt: string;
  variant: CalendarBlockVariant;
  completionStatus: CalendarCompletionStatus;
  isPinned: boolean;
  isProtected: boolean;
  isRecurring: boolean;
  googleEventId?: string | null;
  googleLastSyncedAt?: string | null;
  googleSyncError?: string | null;
  hierarchyDepth: number;
  renderedChildTaskIds: string[];
  children?: CalendarBlockChild[];
}

export interface ApiCalendarBlock {
  id: number;
  taskId: number;
  taskStatus?: CalendarCompletionStatus | 'todo' | 'in-progress' | 'done';
  taskDeadlineAt?: string | null;
  taskPriority?: 'low' | 'medium' | 'high';
  timeEstimateMinutes?: number | null;
  categoryId: number;
  title: string;
  emoji?: string | null;
  categoryName: string;
  categoryColor: string;
  startAt: string;
  endAt: string;
  variant: CalendarBlockVariant;
  completionStatus: CalendarCompletionStatus;
  isPinned: boolean;
  isProtected: boolean;
  isRecurring: boolean;
  googleEventId?: string | null;
  googleLastSyncedAt?: string | null;
  googleSyncError?: string | null;
  hierarchyDepth?: number;
  renderedChildTaskIds?: number[];
}

export interface ManualCalendarBlockCreate {
  taskId: string;
  categoryId: number;
  startAt: string;
  endAt: string;
}

export interface CalendarBlockTimeUpdate {
  startAt: string;
  endAt: string;
}
