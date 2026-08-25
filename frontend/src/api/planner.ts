import type { CalendarBlock } from '../types/calendar';
import type {
  PlannerProposeRequest,
  PlannerProposeResult,
  PlannerScheduleConflict,
  PlannerSchedulePatchProposal,
  SchedulePatch,
  SchedulePatchOperation,
  SchedulePatchSimulation,
} from '../types/planner';
import { apiRequest } from './http';

const BASE = '/api/v1/ai/planner';

function idToString(value: unknown): string {
  if (typeof value === 'number') {
    return value.toString();
  }
  return typeof value === 'string' ? value : '';
}

function numberListToStrings(value: unknown): string[] {
  return Array.isArray(value)
    ? value.map(idToString).filter((id) => id.length > 0)
    : [];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function normalizeCalendarBlock(block: Record<string, unknown>): CalendarBlock {
  return {
    id: String(block.id ?? block.proposalBlockId ?? ''),
    taskId: idToString(block.taskId),
    taskStatus: block.taskStatus as CalendarBlock['taskStatus'],
    taskDeadlineAt: typeof block.taskDeadlineAt === 'string' ? block.taskDeadlineAt : null,
    taskPriority: block.taskPriority as CalendarBlock['taskPriority'],
    timeEstimateMinutes:
      typeof block.timeEstimateMinutes === 'number' ? block.timeEstimateMinutes : null,
    categoryId: Number(block.categoryId ?? 0),
    title: String(block.title ?? 'Proposed task'),
    emoji: typeof block.emoji === 'string' ? block.emoji : null,
    categoryName: String(block.categoryName ?? 'Calendar'),
    categoryColor: String(block.categoryColor ?? '#1f6feb'),
    startAt: String(block.startAt ?? ''),
    endAt: String(block.endAt ?? ''),
    variant: (block.variant as CalendarBlock['variant']) ?? 'proposed',
    completionStatus:
      (block.completionStatus as CalendarBlock['completionStatus']) ?? 'not_started',
    isPinned: Boolean(block.isPinned),
    isProtected: Boolean(block.isProtected),
    isRecurring: Boolean(block.isRecurring),
    hierarchyDepth: typeof block.hierarchyDepth === 'number' ? block.hierarchyDepth : 0,
    renderedChildTaskIds: numberListToStrings(block.renderedChildTaskIds),
  };
}

function normalizeOperation(operation: Record<string, unknown>): SchedulePatchOperation {
  return {
    ...operation,
    taskId: idToString(operation.taskId) || null,
    renderedChildTaskIds: numberListToStrings(operation.renderedChildTaskIds),
  } as SchedulePatchOperation;
}

function normalizePatch(patch: Record<string, unknown>): SchedulePatch {
  return {
    patchId: String(patch.patchId ?? ''),
    intentSummary: String(patch.intentSummary ?? ''),
    targetScope: String(patch.targetScope ?? ''),
    workingSetTaskIds: numberListToStrings(patch.workingSetTaskIds),
    windowStart: String(patch.windowStart ?? ''),
    windowEnd: String(patch.windowEnd ?? ''),
    operations: Array.isArray(patch.operations)
      ? patch.operations
          .filter((item): item is Record<string, unknown> => item !== null && typeof item === 'object')
          .map(normalizeOperation)
      : [],
  };
}

function normalizeSimulation(simulation: Record<string, unknown>): SchedulePatchSimulation {
  return {
    ...simulation,
    operationCount: Number(simulation.operationCount ?? 0),
    affectedTaskIds: numberListToStrings(simulation.affectedTaskIds),
    affectedCalendarBlockIds: Array.isArray(simulation.affectedCalendarBlockIds)
      ? simulation.affectedCalendarBlockIds.filter((id): id is number => typeof id === 'number')
      : [],
    previewBlocks: Array.isArray(simulation.previewBlocks)
      ? simulation.previewBlocks
          .filter((item): item is Record<string, unknown> => item !== null && typeof item === 'object')
          .map(normalizeCalendarBlock)
      : [],
    skippedTaskIds: numberListToStrings(simulation.skippedTaskIds),
    conflicts: Array.isArray(simulation.conflicts)
      ? simulation.conflicts
          .filter((item): item is Record<string, unknown> => item !== null && typeof item === 'object')
          .map(normalizeConflict)
      : [],
    warnings: Array.isArray(simulation.warnings)
      ? simulation.warnings.filter((item): item is string => typeof item === 'string')
      : [],
  } as SchedulePatchSimulation;
}

function normalizeConflict(conflict: Record<string, unknown>): PlannerScheduleConflict {
  return {
    taskId: idToString(conflict.taskId),
    taskTitle: typeof conflict.taskTitle === 'string' ? conflict.taskTitle : null,
    type: String(conflict.type ?? ''),
    detail: String(conflict.detail ?? ''),
    conflictingBlockIds: Array.isArray(conflict.conflictingBlockIds)
      ? conflict.conflictingBlockIds.filter((id): id is number => typeof id === 'number')
      : [],
    conflictingBlocks: Array.isArray(conflict.conflictingBlocks)
      ? conflict.conflictingBlocks
          .filter((item): item is Record<string, unknown> => item !== null && typeof item === 'object')
          .map((item) => ({
            blockId: Number(item.blockId ?? 0),
            taskId: idToString(item.taskId) || null,
            startAt: String(item.startAt ?? ''),
            endAt: String(item.endAt ?? ''),
            state: String(item.state ?? 'occupied'),
            label: typeof item.label === 'string' ? item.label : null,
          }))
      : [],
    alternatives: Array.isArray(conflict.alternatives)
      ? conflict.alternatives
          .filter((item): item is Record<string, unknown> => item !== null && typeof item === 'object')
          .map((item) => ({
            startAt: String(item.startAt ?? ''),
            endAt: String(item.endAt ?? ''),
            reason: String(item.reason ?? ''),
          }))
      : [],
  };
}

export function normalizePlannerScheduleProposal(
  proposal: Record<string, unknown> | null | undefined,
): PlannerSchedulePatchProposal | null {
  if (!proposal || !isRecord(proposal.schedulePatch)
    || !isRecord(proposal.schedulePatchValidation)
    || !isRecord(proposal.schedulePatchSimulation)) {
    return null;
  }
  const rawPayload = isRecord(proposal.payload) ? proposal.payload : undefined;
  const conflicts = Array.isArray(rawPayload?.conflicts)
    ? rawPayload.conflicts
        .filter((item): item is Record<string, unknown> => item !== null && typeof item === 'object')
        .map(normalizeConflict)
    : [];
  return {
    proposalId: String(proposal.proposalId ?? ''),
    proposalKind: String(proposal.proposalKind ?? 'schedule_patch'),
    title: String(proposal.title ?? 'Schedule proposal'),
    body: String(proposal.body ?? ''),
    summary: typeof proposal.summary === 'string' ? proposal.summary : null,
    preview: Array.isArray(proposal.preview)
      ? proposal.preview.filter((item): item is string => typeof item === 'string')
      : [],
    validationWarnings: Array.isArray(proposal.validationWarnings)
      ? proposal.validationWarnings.filter((item): item is string => typeof item === 'string')
      : [],
    schedulePatch: normalizePatch(proposal.schedulePatch),
    schedulePatchValidation: {
      ...(proposal.schedulePatchValidation as Record<string, unknown>),
      status: String(
        proposal.schedulePatchValidation.status ?? 'invalid',
      ),
      errors: Array.isArray(proposal.schedulePatchValidation.errors)
        ? (proposal.schedulePatchValidation.errors as unknown[]).filter(
            (item): item is string => typeof item === 'string',
          )
        : [],
      warnings: Array.isArray(proposal.schedulePatchValidation.warnings)
        ? (proposal.schedulePatchValidation.warnings as unknown[]).filter(
            (item): item is string => typeof item === 'string',
          )
        : [],
      operationCount: Number(proposal.schedulePatchValidation.operationCount ?? 0),
    },
    schedulePatchSimulation: normalizeSimulation(proposal.schedulePatchSimulation),
    schedulePatchStatus: String(proposal.schedulePatchStatus ?? 'invalid'),
    payload: {
      ...rawPayload,
      conflicts,
    },
  };
}

export async function proposeSchedulePatch(
  data: PlannerProposeRequest & { windowStart: string; windowEnd: string },
): Promise<PlannerProposeResult> {
  const response = await apiRequest<PlannerProposeResult>(`${BASE}/propose`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      prompt: data.prompt,
      workingSetTaskIds: data.workingSetTaskIds.map((id) => parseInt(id, 10)),
      windowStart: data.windowStart,
      windowEnd: data.windowEnd,
      slotMinutes: data.slotMinutes,
      fallbackDurationMinutes: data.fallbackDurationMinutes,
      providerAuthPreflight: data.providerAuthPreflight,
      operationMode: data.operationMode,
    }),
  });
  return {
    ...response,
    proposal: normalizePlannerScheduleProposal(response.proposal as unknown as Record<string, unknown> | null),
    diagnostics: {
      ...response.diagnostics,
      fallbackDurationTaskIds: numberListToStrings(response.diagnostics?.fallbackDurationTaskIds),
      alreadyScheduledTaskIds: numberListToStrings(
        response.diagnostics?.alreadyScheduledTaskIds,
      ),
    },
  };
}
