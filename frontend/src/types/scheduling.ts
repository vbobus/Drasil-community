import type { CalendarBlock } from './calendar';

export interface AutoScheduleDraftRequest {
  taskIds: string[];
  windowStart?: string;
  windowEnd?: string;
  slotMinutes?: number;
  fallbackDurationMinutes?: number;
}

export interface ApiDraftCalendarBlock {
  id: string;
  taskId: number;
  taskStatus?: CalendarBlock['taskStatus'];
  taskDeadlineAt?: string | null;
  taskPriority?: CalendarBlock['taskPriority'];
  timeEstimateMinutes?: number | null;
  categoryId: number;
  title: string;
  emoji?: string | null;
  categoryName: string;
  categoryColor: string;
  startAt: string;
  endAt: string;
  variant: 'draft';
  completionStatus: CalendarBlock['completionStatus'];
  isPinned: boolean;
  isProtected: boolean;
  isRecurring: boolean;
  hierarchyDepth?: number;
  renderedChildTaskIds?: number[];
}

export interface ScheduleAlternative {
  startAt: string;
  endAt: string;
  reason: string;
}

export interface ApiScheduleConflict {
  taskId: number;
  type: string;
  detail: string;
  conflictingBlockIds: number[];
  alternatives: ScheduleAlternative[];
}

export interface ScheduleConflict {
  taskId: string;
  type: string;
  detail: string;
  conflictingBlockIds: number[];
  alternatives: ScheduleAlternative[];
}

export interface ScheduleDiagnostics {
  solverStatus: 'feasible' | 'partial' | 'conflict' | string;
  runtimeMs: number;
  scheduledTaskCount: number;
  conflictCount: number;
  occupiedBlockCount: number;
  fallbackDurationTaskIds: string[];
  alreadyScheduledTaskIds: string[];
}

export interface ApiScheduleDiagnostics {
  solverStatus: string;
  runtimeMs: number;
  scheduledTaskCount: number;
  conflictCount: number;
  occupiedBlockCount: number;
  fallbackDurationTaskIds: number[];
  alreadyScheduledTaskIds?: number[];
}

export interface ApiAutoScheduleDraftResponse {
  draftBlocks: ApiDraftCalendarBlock[];
  conflicts: ApiScheduleConflict[];
  diagnostics: ApiScheduleDiagnostics;
}

export interface AutoScheduleDraftResult {
  draftBlocks: CalendarBlock[];
  conflicts: ScheduleConflict[];
  diagnostics: ScheduleDiagnostics;
}
