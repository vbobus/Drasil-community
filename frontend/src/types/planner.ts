import type { CalendarBlock } from './calendar';

export interface PlannerProposeRequest {
  prompt: string;
  workingSetTaskIds: string[];
  windowStart?: string;
  windowEnd?: string;
  slotMinutes?: number;
  fallbackDurationMinutes?: number;
  providerAuthPreflight?: boolean;
  operationMode?: 'create' | 'reschedule' | 'remove';
}

export interface PlannerContextCard {
  id: string;
  label: string;
  value: string;
  detail?: string | null;
}

export interface SchedulePatchOperation {
  op: string;
  proposalBlockId?: string | null;
  acceptedBlockId?: number | null;
  taskId?: string | null;
  categoryId?: number | null;
  title?: string | null;
  emoji?: string | null;
  categoryName?: string | null;
  categoryColor?: string | null;
  startAt?: string | null;
  endAt?: string | null;
  variant?: CalendarBlock['variant'];
  completionStatus?: CalendarBlock['completionStatus'];
  taskStatus?: CalendarBlock['taskStatus'];
  taskDeadlineAt?: string | null;
  taskPriority?: CalendarBlock['taskPriority'];
  timeEstimateMinutes?: number | null;
  allocatedTimeboxMinutes?: number | null;
  isPinned?: boolean;
  isProtected?: boolean;
  isRecurring?: boolean;
  hierarchyDepth?: number;
  renderedChildTaskIds?: string[];
  source?: string | null;
}

export interface SchedulePatch {
  patchId: string;
  intentSummary: string;
  targetScope: string;
  workingSetTaskIds: string[];
  windowStart: string;
  windowEnd: string;
  operations: SchedulePatchOperation[];
}

export interface SchedulePatchValidation {
  status: 'valid' | 'warning' | 'invalid' | string;
  errors: string[];
  warnings: string[];
  operationCount: number;
}

export interface SchedulePatchSimulation {
  operationCount: number;
  affectedTaskIds: string[];
  affectedCalendarBlockIds: number[];
  previewBlocks: CalendarBlock[];
  skippedTaskIds?: string[];
  createBlocks?: number;
  updateBlockTimes?: number;
  removeBlocks?: number;
  skipTasks?: number;
  conflicts?: PlannerScheduleConflict[];
  warnings?: string[];
}

export interface PlannerScheduleConflict {
  taskId: string;
  taskTitle?: string | null;
  type: string;
  detail: string;
  conflictingBlockIds: number[];
  conflictingBlocks?: Array<{
    blockId: number;
    taskId?: string | null;
    startAt: string;
    endAt: string;
    state: string;
    label?: string | null;
  }>;
  alternatives: Array<{
    startAt: string;
    endAt: string;
    reason: string;
  }>;
}

export interface PlannerSchedulePatchProposal {
  proposalId: string;
  proposalKind: 'schedule_patch' | string;
  title: string;
  body: string;
  summary?: string | null;
  preview: string[];
  validationWarnings: string[];
  schedulePatch: SchedulePatch;
  schedulePatchValidation: SchedulePatchValidation;
  schedulePatchSimulation: SchedulePatchSimulation;
  schedulePatchStatus: 'valid' | 'warning' | 'invalid' | string;
  payload?: {
    conflicts?: PlannerScheduleConflict[];
    solverStatus?: string | null;
    workingSet?: Array<Record<string, unknown>>;
    [key: string]: unknown;
  } | null;
}

export interface PlannerDiagnostics {
  controlPlane: string;
  providerReadiness?: Record<string, unknown>;
  solverStatus?: string | null;
  runtimeMs?: number | null;
  fallbackDurationTaskIds?: string[];
  alreadyScheduledTaskIds?: string[];
  conflictCount?: number;
  acceptedBlockCount?: number;
  [key: string]: unknown;
}

export interface PlannerProposeResult {
  providerLabel: string;
  mode: 'schedule_patch' | 'needs_configuration' | string;
  summary: string;
  contextCards: PlannerContextCard[];
  suggestedPrompts: string[];
  proposal: PlannerSchedulePatchProposal | null;
  diagnostics: PlannerDiagnostics;
}
