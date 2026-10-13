import { useEffect, useMemo, useRef, useState } from 'react';
import { AssistantRuntimeProvider, useExternalStoreRuntime, ThreadPrimitive, MessagePrimitive, ComposerPrimitive, ActionBarPrimitive, type ThreadMessageLike } from '@assistant-ui/react';
import { ArrowDown, ArrowUp, Check, Copy, Pencil, Search, RotateCcw, Square, X } from 'lucide-react';
import type { Category } from '../../types/category';
import type { StrategyNode } from '../../types/node';
import {
  createStrategistRunEventSource,
  fetchStrategistRun,
  fetchStrategistSessionDetail,
  fetchStrategistSessions,
  requestStrategistAdvice,
  submitStrategistReviewAction,
  type StrategistBadgeSuggestion,
  type StrategistClarificationChoice,
  type StrategistClarificationConflict,
  type StrategistClarificationPayload,
  type StrategistResponse,
  type StrategistReviewAction,
  type StrategistReviewActionOutcome,
  type StrategistReviewDocument,
  type StrategistRunResponse,
  type StrategistRunActivity,
  type StrategistSessionSummary,
  type StrategistStandardProposal,
  type StrategistTaskReference,
  type StrategistTrace,
  type StrategistTraceStep,
  type StrategistTurnRecord,
  type StrategistUiEvent,
} from '../../api/strategist';
import { useNodeStore } from '../../store/useNodeStore';
import drasilLogoUrl from '../../assets/drasil-logo-transparent.png';
import './StrategistAiCard.css';
import { ChatMarkdown } from '../ai/ChatMarkdown';
import { ChatProgress } from '../ai/ChatProgress';
import { useUiStore } from '../../store/useUiStore';
import type { AiWorkspaceLaunchRequest } from '../../App';
import { useCalendarStore } from '../../store/useCalendarStore';
import { refreshWorkspace, useWorkspaceStore } from '../../store/useWorkspaceStore';
import { useCategoryStore } from '../../store/useCategoryStore';
import { normalizePlannerScheduleProposal } from '../../api/planner';
import { RichAiText, type AiTaskReference } from '../ai/RichAiText';
import { suggestEmoji } from '../../lib/emoji';
import { ReviewDocumentCard } from './ReviewDocumentCard';

const RETIRED_COMPATIBILITY_ACTIONS_ENABLED = false;

interface StrategistAiCardProps {
  embedded?: boolean;
  activeCategory: Category | null;
  currentTask: StrategyNode | null;
  stagedNodeIds: string[];
  isExpanded: boolean;
  onToggleExpanded: () => void;
  onClose: () => void;
  launchRequest: AiWorkspaceLaunchRequest | null;
  onLaunchHandled: (requestId: string) => void;
  onRunUpdate?: (result: StrategistRunResponse) => void;
  onSessionActivity?: (sessionId: string, turnId: string) => void;
}

interface SharedResultGuard {
  generation: number;
  stagedNodeIds: string[];
  calendarRangeKey: string;
}

function matchCase(replacement: string, source: string) {
  return source.charAt(0) === source.charAt(0).toUpperCase()
    ? `${replacement.charAt(0).toUpperCase()}${replacement.slice(1)}`
    : replacement;
}

function formatWorkspaceCopy(value: string) {
  return value
    .replace(/\bmetadata proposal(s)?\b/gi, (match, plural: string | undefined) =>
      matchCase(`change proposal${plural ? 's' : ''}`, match),
    )
    .replace(/\bmetadata update(s)?\b/gi, (match, plural: string | undefined) =>
      matchCase(`task detail update${plural ? 's' : ''}`, match),
    )
    .replace(/\bmetadata change(s)?\b/gi, (match, plural: string | undefined) =>
      matchCase(`task detail change${plural ? 's' : ''}`, match),
    )
    .replace(/\bmetadata field(s)?\b/gi, (match, plural: string | undefined) =>
      matchCase(`task detail field${plural ? 's' : ''}`, match),
    )
    .replace(/\bmetadata\b/gi, (match) => matchCase('task details', match));
}

type TurnActionStatus =
  | 'idle'
  | 'executing'
  | 'confirmed'
  | 'dismissed'
  | 'revised'
  | 'stale'
  | 'failed';

const TERMINAL_RUN_STATUSES = new Set(['completed', 'failed', 'cancelled']);

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => {
    window.setTimeout(resolve, ms);
  });
}

function parseStrategistRunEvent(event: MessageEvent<string>): StrategistRunResponse | null {
  try {
    const parsed = JSON.parse(event.data) as unknown;
    if (parsed && typeof parsed === 'object' && 'turn' in parsed && 'session' in parsed) {
      return parsed as StrategistRunResponse;
    }
  } catch {
    return null;
  }
  return null;
}

interface StrategistChatTurn {
  id: string;
  prompt: string;
  reasoning?: string;
  streamedAnswer?: string;
  response: StrategistResponse | null;
  error: string | null;
  actionStatus: TurnActionStatus;
  actionError: string | null;
  reviewActionId: string | null;
  reviewOutcome: StrategistReviewActionOutcome | null;
  isHistorical: boolean;
  createdAt?: string;
}

function createTurnId() {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) {
    return crypto.randomUUID();
  }
  return `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function formatSessionTime(value: string) {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return '';
  }

  return new Intl.DateTimeFormat('en-US', {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  }).format(parsed);
}

const WEEKDAY_LABELS = [
  'Sunday',
  'Monday',
  'Tuesday',
  'Wednesday',
  'Thursday',
  'Friday',
  'Saturday',
] as const;

const WEEKDAY_INDEX = new Map(
  WEEKDAY_LABELS.map((label, index) => [label.toLocaleLowerCase(), index]),
);

function parseScheduleDate(value: string) {
  if (!value) return null;
  const normalized = /^\d{4}-\d{2}-\d{2}$/.test(value)
    ? `${value}T12:00:00`
    : value;
  const parsed = new Date(normalized);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

function formatClockOnly(value: string) {
  const parsed = parseScheduleDate(value);
  if (!parsed) return '';
  return new Intl.DateTimeFormat('en-US', {
    hour: 'numeric',
    minute: '2-digit',
  }).format(parsed);
}

function formatShortDate(value: string, includeYear = false) {
  const parsed = parseScheduleDate(value);
  if (!parsed) return '';
  return new Intl.DateTimeFormat('en-US', {
    month: 'short',
    day: 'numeric',
    ...(includeYear ? { year: 'numeric' as const } : {}),
  }).format(parsed);
}

function joinNatural(values: string[]) {
  if (values.length <= 1) return values[0] ?? '';
  if (values.length === 2) return `${values[0]} and ${values[1]}`;
  return `${values.slice(0, -1).join(', ')}, and ${values.at(-1)}`;
}

function weekdayPatternFromNames(values: string[], prefix = 'Every') {
  const indexes = Array.from(new Set(
    values
      .map((value) => WEEKDAY_INDEX.get(value.trim().toLocaleLowerCase()))
      .filter((value): value is number => value != null),
  )).sort((left, right) => ((left + 6) % 7) - ((right + 6) % 7));
  if (indexes.length === 0) return '';
  if (indexes.join(',') === '1,2,3,4,5') return prefix === 'Every' ? 'Weekdays' : 'Mondays through Fridays';
  if (indexes.length === 2 && indexes.includes(0) && indexes.includes(6)) {
    return prefix === 'Every' ? 'Weekends' : 'Saturdays and Sundays';
  }
  const labels = indexes.map((index) => WEEKDAY_LABELS[index]);
  return `${prefix} ${joinNatural(labels)}`;
}

function ordinalWord(value: number) {
  return ['first', 'second', 'third', 'fourth', 'fifth'][value - 1] ?? `${value}th`;
}

function inferAffectedPattern(values: string[]) {
  const dates = values
    .map(parseScheduleDate)
    .filter((value): value is Date => value !== null)
    .sort((left, right) => left.getTime() - right.getTime());
  if (dates.length === 0) return '';
  if (dates.length === 1) {
    return new Intl.DateTimeFormat('en-US', {
      weekday: 'short',
      month: 'short',
      day: 'numeric',
    }).format(dates[0]);
  }
  const monthKeys = new Set(dates.map((dateValue) => `${dateValue.getFullYear()}-${dateValue.getMonth()}`));
  const monthDays = new Set(dates.map((dateValue) => dateValue.getDate()));
  const monthIndexes = dates.map((dateValue) => dateValue.getFullYear() * 12 + dateValue.getMonth());
  const consecutiveMonths = monthIndexes.every(
    (monthIndex, index) => index === 0 || monthIndex === monthIndexes[index - 1] + 1,
  );
  if (monthKeys.size === dates.length && monthDays.size === 1 && consecutiveMonths) {
    return `Every month on the ${ordinalWord(dates[0].getDate())}`;
  }
  const weekdayOrdinals = new Set(
    dates.map((dateValue) => Math.ceil(dateValue.getDate() / 7)),
  );
  const weekdays = new Set(dates.map((dateValue) => dateValue.getDay()));
  if (
    monthKeys.size === dates.length
    && weekdays.size === 1
    && weekdayOrdinals.size === 1
    && consecutiveMonths
  ) {
    return `Every ${ordinalWord(Array.from(weekdayOrdinals)[0])} ${WEEKDAY_LABELS[dates[0].getDay()]}`;
  }
  const actualDateKeys = new Set(dates.map((dateValue) =>
    `${dateValue.getFullYear()}-${dateValue.getMonth()}-${dateValue.getDate()}`));
  const cursor = new Date(dates[0]);
  const lastDate = dates.at(-1) as Date;
  let expectedWeeklyDates = 0;
  let scannedDays = 0;
  while (cursor <= lastDate && scannedDays <= 370) {
    if (weekdays.has(cursor.getDay())) {
      const key = `${cursor.getFullYear()}-${cursor.getMonth()}-${cursor.getDate()}`;
      if (!actualDateKeys.has(key)) {
        expectedWeeklyDates = -1;
        break;
      }
      expectedWeeklyDates += 1;
    }
    cursor.setDate(cursor.getDate() + 1);
    scannedDays += 1;
  }
  if (expectedWeeklyDates === dates.length) {
    return weekdayPatternFromNames(
      Array.from(weekdays).map((index) => WEEKDAY_LABELS[index]),
      '',
    ).trim();
  }
  const shown = dates.slice(0, 3).map((dateValue) => new Intl.DateTimeFormat('en-US', {
    month: 'short',
    day: 'numeric',
  }).format(dateValue));
  return dates.length > shown.length
    ? `${joinNatural(shown)}, and ${dates.length - shown.length} more dates`
    : joinNatural(shown);
}

function formatSchedulePeriod(startValue: string, endValue: string, ongoing = false) {
  if (!startValue) return '';
  if (ongoing) return `Starting ${formatShortDate(startValue, true)} · ongoing`;
  if (!endValue || startValue.slice(0, 10) === endValue.slice(0, 10)) {
    return formatShortDate(startValue, true);
  }
  const start = parseScheduleDate(startValue);
  const end = parseScheduleDate(endValue);
  const sameYear = Boolean(start && end && start.getFullYear() === end.getFullYear());
  return `${formatShortDate(startValue, !sameYear)} – ${formatShortDate(endValue, true)}`;
}

interface CompactMovedSchedule {
  identity: string;
  title: string;
  originalStartAt: string;
  originalEndAt: string;
  movedStartAt: string;
  movedEndAt: string;
  dates: string[];
}

function compactMovedSchedules(resolutions: Record<string, unknown>[]) {
  const groups = new Map<string, CompactMovedSchedule>();
  resolutions.forEach((resolution) => {
    const blockers = Array.isArray(resolution.blockers)
      ? resolution.blockers.filter(isRecord)
      : [];
    blockers.forEach((blocker) => {
      const identity = typeof blocker.taskId === 'string' || typeof blocker.taskId === 'number'
        ? String(blocker.taskId)
        : typeof blocker.task_id === 'string' || typeof blocker.task_id === 'number'
          ? String(blocker.task_id)
          : '';
      const title = typeof blocker.taskTitle === 'string' ? blocker.taskTitle : 'Existing task';
      const originalStartAt = typeof blocker.startAt === 'string' ? blocker.startAt : '';
      const originalEndAt = typeof blocker.endAt === 'string' ? blocker.endAt : '';
      const movedStartAt = typeof blocker.movedStartAt === 'string' ? blocker.movedStartAt : '';
      const movedEndAt = typeof blocker.movedEndAt === 'string' ? blocker.movedEndAt : '';
      if (!originalStartAt || !originalEndAt || !movedStartAt || !movedEndAt) return;
      const key = [
        identity,
        title,
        formatClockOnly(originalStartAt),
        formatClockOnly(originalEndAt),
        formatClockOnly(movedStartAt),
        formatClockOnly(movedEndAt),
      ].join('|');
      const existing = groups.get(key);
      const dateValue = typeof resolution.date === 'string'
        ? resolution.date
        : originalStartAt.slice(0, 10);
      if (existing) {
        if (!existing.dates.includes(dateValue)) existing.dates.push(dateValue);
      } else {
        groups.set(key, {
          identity,
          title,
          originalStartAt,
          originalEndAt,
          movedStartAt,
          movedEndAt,
          dates: [dateValue],
        });
      }
    });
  });
  return Array.from(groups.values()).map((group) => ({
    ...group,
    dates: group.dates.sort(),
    pattern: inferAffectedPattern(group.dates),
    period: formatSchedulePeriod(group.dates[0], group.dates.at(-1) ?? group.dates[0]),
  }));
}

interface CompactClarificationConflict extends StrategistClarificationConflict {
  dates: string[];
  pattern: string;
  period: string;
}

function compactClarificationConflicts(conflicts: StrategistClarificationConflict[]) {
  const groups = new Map<string, CompactClarificationConflict>();
  conflicts.forEach((conflict) => {
    const key = [
      conflict.incomingTaskTitle,
      conflict.blockerTaskTitle,
      conflict.incomingPriority ?? '',
      conflict.blockerPriority ?? '',
      formatClockOnly(conflict.requestedStartAt),
      formatClockOnly(conflict.requestedEndAt),
      formatClockOnly(conflict.blockerStartAt),
      formatClockOnly(conflict.blockerEndAt),
      conflict.fixedReason ?? '',
      conflict.decisionReason ?? '',
      conflict.recommendedAction ?? '',
    ].join('|');
    const dateValue = conflict.date ?? conflict.requestedStartAt.slice(0, 10);
    const existing = groups.get(key);
    if (existing) {
      if (!existing.dates.includes(dateValue)) existing.dates.push(dateValue);
      return;
    }
    groups.set(key, {
      ...conflict,
      dates: [dateValue],
      pattern: '',
      period: '',
    });
  });
  return Array.from(groups.values()).map((group) => {
    const dates = group.dates.sort();
    return {
      ...group,
      dates,
      pattern: inferAffectedPattern(dates),
      period: formatSchedulePeriod(dates[0], dates.at(-1) ?? dates[0]),
    };
  });
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function getArrayField<T = unknown>(
  value: Record<string, unknown>,
  camelKey: string,
  snakeKey: string,
): T[] {
  const camelValue = value[camelKey];
  if (Array.isArray(camelValue)) {
    return camelValue as T[];
  }
  const snakeValue = value[snakeKey];
  if (Array.isArray(snakeValue)) {
    return snakeValue as T[];
  }
  return [];
}

function getObjectField<T>(
  value: Record<string, unknown>,
  camelKey: string,
  snakeKey: string,
): T | null {
  const camelValue = value[camelKey];
  if (isRecord(camelValue)) {
    return camelValue as T;
  }
  const snakeValue = value[snakeKey];
  if (isRecord(snakeValue)) {
    return snakeValue as T;
  }
  return null;
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === 'string');
}

function isReviewEntityValue(value: unknown) {
  return isRecord(value)
    && typeof value.label === 'string'
    && isRecord(value.ref)
    && typeof value.ref.kind === 'string'
    && typeof value.ref.entityId === 'string';
}

function isReviewChangeValue(value: unknown) {
  return isRecord(value)
    && typeof value.changeId === 'string'
    && typeof value.summary === 'string'
    && Array.isArray(value.entities)
    && value.entities.every(isReviewEntityValue)
    && (
      value.supportingOperationIds == null
      || isStringArray(value.supportingOperationIds)
    )
    && (value.before == null || typeof value.before === 'string')
    && (value.after == null || typeof value.after === 'string');
}

function isReviewBlockValue(value: unknown) {
  if (
    !isRecord(value)
    || typeof value.kind !== 'string'
    || typeof value.blockId !== 'string'
    || typeof value.title !== 'string'
  ) return false;

  switch (value.kind) {
    case 'entity_references':
      return Array.isArray(value.entities) && value.entities.every(isReviewEntityValue);
    case 'grouped_changes':
      return Array.isArray(value.groups) && value.groups.every((group) =>
        isRecord(group)
        && typeof group.groupId === 'string'
        && typeof group.summary === 'string'
        && Array.isArray(group.changes)
        && group.changes.every(isReviewChangeValue));
    case 'semantic_diff':
      return Array.isArray(value.changes) && value.changes.every(isReviewChangeValue);
    case 'schedule_summary':
      return typeof value.cadence === 'string'
        && typeof value.rangeLabel === 'string'
        && isStringArray(value.details);
    case 'recurrence_summary':
      return typeof value.cadence === 'string'
        && typeof value.rangeLabel === 'string'
        && typeof value.scopeLabel === 'string'
        && isStringArray(value.exceptions);
    case 'conflict_resolution':
      return Array.isArray(value.conflicts) && value.conflicts.every((conflict) =>
        isRecord(conflict)
        && typeof conflict.conflictId === 'string'
        && typeof conflict.summary === 'string'
        && typeof conflict.resolution === 'string'
        && Array.isArray(conflict.affectedEntities)
        && conflict.affectedEntities.every(isReviewEntityValue)
        && isStringArray(conflict.intervals)
        && isStringArray(conflict.alternatives));
    case 'assumptions':
      return isStringArray(value.statements);
    case 'warning':
      return ['info', 'warning', 'critical'].includes(String(value.severity))
        && typeof value.message === 'string';
    case 'semantic_fallback':
      return typeof value.summary === 'string'
        && Array.isArray(value.effects)
        && value.effects.every(isReviewChangeValue);
    case 'execution_status':
      return typeof value.summary === 'string'
        && typeof value.verificationSummary === 'string'
        && typeof value.replayed === 'boolean'
        && Array.isArray(value.bindings)
        && Array.isArray(value.facts)
        && Array.isArray(value.syncItems)
        && value.syncItems.every((sync) =>
          isRecord(sync)
          && typeof sync.syncId === 'string'
          && typeof sync.providerLabel === 'string'
          && typeof sync.status === 'string'
          && typeof sync.summary === 'string'
          && typeof sync.attempts === 'number');
    default:
      return true;
  }
}

function isReviewActionValue(value: unknown) {
  if (
    !isRecord(value)
    || !['approve', 'edit', 'reject', 'decision', 'retry'].includes(String(value.action))
    || typeof value.eventId !== 'string'
    || typeof value.idempotencyKey !== 'string'
    || typeof value.reviewId !== 'string'
    || typeof value.reviewRevision !== 'number'
    || !isRecord(value.binding)
  ) return false;
  if (value.action === 'approve') return typeof value.commandId === 'string';
  if (value.action === 'decision') {
    return typeof value.decisionId === 'string' && typeof value.optionId === 'string';
  }
  if (value.action === 'retry') {
    return typeof value.executionId === 'string'
      && typeof value.originalCommandId === 'string'
      && typeof value.originalIdempotencyKey === 'string';
  }
  return value.action !== 'edit' || value.correction == null || typeof value.correction === 'string';
}

function normalizeReviewDocumentForUi(value: unknown): StrategistReviewDocument | null {
  if (!isRecord(value)) return null;
  const requiredStrings = [
    'reviewId',
    'sessionId',
    'goalId',
    'changeSetId',
    'simulationId',
    'simulationInputFingerprint',
    'simulationOutputFingerprint',
    'correlationId',
    'causationId',
    'summary',
  ];
  if (requiredStrings.some((key) => typeof value[key] !== 'string')) return null;
  if (
    typeof value.revision !== 'number'
    || typeof value.goalRevision !== 'number'
    || typeof value.changeSetRevision !== 'number'
    || !Array.isArray(value.blocks)
    || !value.blocks.every(isReviewBlockValue)
    || !Array.isArray(value.decisions)
    || !value.decisions.every((decision) =>
      isRecord(decision)
      && typeof decision.decisionId === 'string'
      && typeof decision.question === 'string'
      && typeof decision.reason === 'string'
      && Array.isArray(decision.options)
      && decision.options.every((option) =>
        isRecord(option)
        && typeof option.optionId === 'string'
        && typeof option.label === 'string'))
    || !Array.isArray(value.controls)
    || !value.controls.every(isReviewActionValue)
  ) return null;
  return value as unknown as StrategistReviewDocument;
}

function normalizeTraceStepForUi(value: unknown): StrategistTraceStep | null {
  if (!isRecord(value)) {
    return null;
  }
  const title = getStringField(value, 'title', 'title') ?? 'Trace step';
  const detail = getStringField(value, 'detail', 'detail') ?? '';
  const toolName = getStringField(value, 'toolName', 'tool_name');
  const durationValue = value.durationMs ?? value.duration_ms;
  return {
    kind: getStringField(value, 'kind', 'kind') ?? 'decision',
    title,
    detail,
    durationMs:
      typeof durationValue === 'number' && Number.isFinite(durationValue)
        ? durationValue
        : null,
    toolName,
  };
}

function normalizeClarificationChoiceForUi(
  value: unknown,
): StrategistClarificationChoice | null {
  if (!isRecord(value)) {
    return null;
  }
  const label = getStringField(value, 'label', 'label');
  const rawValue = getStringField(value, 'value', 'value');
  const followUpPrompt = getStringField(value, 'followUpPrompt', 'follow_up_prompt');
  const goalId = getStringField(value, 'goalId', 'goal_id');
  const decisionId = getStringField(value, 'decisionId', 'decision_id');
  const optionId = getStringField(value, 'optionId', 'option_id');
  const goalRevisionValue = value.goalRevision ?? value.goal_revision;
  const goalRevision =
    typeof goalRevisionValue === 'number' && Number.isInteger(goalRevisionValue)
      ? goalRevisionValue
      : null;
  const hasAnyTypedIdentity = Boolean(
    goalId || decisionId || optionId || goalRevisionValue !== undefined,
  );
  const hasTypedDecision = Boolean(
    goalId && decisionId && optionId && goalRevision && goalRevision > 0,
  );
  if (hasAnyTypedIdentity && !hasTypedDecision) {
    return null;
  }
  if (!label || !rawValue || (!followUpPrompt && !hasTypedDecision)) {
    return null;
  }
  const kind: 'field' | 'entity' = getStringField(value, 'kind', 'kind') === 'field'
    ? 'field'
    : 'entity';
  const targetNodeId = value.targetNodeId ?? value.target_node_id;
  const base = {
    kind,
    label,
    value: rawValue,
    description: getStringField(value, 'description', 'description'),
    targetNodeId: typeof targetNodeId === 'number' ? targetNodeId : null,
  };
  if (hasTypedDecision) {
    return {
      ...base,
      followUpPrompt: followUpPrompt ?? '',
      goalId: goalId!,
      goalRevision: goalRevision!,
      decisionId: decisionId!,
      optionId: optionId!,
    };
  }
  return { ...base, followUpPrompt: followUpPrompt! };
}

function normalizeClarificationConflictForUi(
  value: unknown,
): StrategistClarificationConflict | null {
  if (!isRecord(value)) {
    return null;
  }
  const incomingTaskTitle = getStringField(
    value,
    'incomingTaskTitle',
    'incoming_task_title',
  );
  const requestedStartAt = getStringField(value, 'requestedStartAt', 'requested_start_at');
  const requestedEndAt = getStringField(value, 'requestedEndAt', 'requested_end_at');
  const blockerTaskTitle = getStringField(
    value,
    'blockerTaskTitle',
    'blocker_task_title',
  );
  const blockerStartAt = getStringField(value, 'blockerStartAt', 'blocker_start_at');
  const blockerEndAt = getStringField(value, 'blockerEndAt', 'blocker_end_at');
  if (
    !incomingTaskTitle
    || !requestedStartAt
    || !requestedEndAt
    || !blockerTaskTitle
    || !blockerStartAt
    || !blockerEndAt
  ) {
    return null;
  }
  return {
    date: getStringField(value, 'date', 'date'),
    incomingTaskTitle,
    incomingPriority: getStringField(value, 'incomingPriority', 'incoming_priority'),
    requestedStartAt,
    requestedEndAt,
    blockerTaskTitle,
    blockerPriority: getStringField(value, 'blockerPriority', 'blocker_priority'),
    blockerStartAt,
    blockerEndAt,
    fixedReason: getStringField(value, 'fixedReason', 'fixed_reason'),
    decisionReason: getStringField(value, 'decisionReason', 'decision_reason'),
    recommendedAction: getStringField(value, 'recommendedAction', 'recommended_action'),
  };
}

function normalizeClarificationForUi(value: unknown): StrategistClarificationPayload | null {
  if (!isRecord(value)) {
    return null;
  }
  const message = getStringField(value, 'message', 'message');
  if (!message) {
    return null;
  }
  const seenFieldChoiceLabels = new Set<string>();
  const fieldChoices = getArrayField(value, 'fieldChoices', 'field_choices')
    .map(normalizeClarificationChoiceForUi)
    .filter((choice): choice is StrategistClarificationChoice => choice !== null)
    .filter((choice) => {
      const labelKey = choice.label.trim().toLocaleLowerCase();
      if (!labelKey || seenFieldChoiceLabels.has(labelKey)) return false;
      seenFieldChoiceLabels.add(labelKey);
      return true;
    });
  return {
    message,
    entityChoices: getArrayField(value, 'entityChoices', 'entity_choices')
      .map(normalizeClarificationChoiceForUi)
      .filter((choice): choice is StrategistClarificationChoice => choice !== null),
    fieldChoices,
    scopedFollowUpPrompts: getArrayField(value, 'scopedFollowUpPrompts', 'scoped_follow_up_prompts')
      .filter((item): item is string => typeof item === 'string' && item.trim().length > 0),
    conflicts: getArrayField(value, 'conflicts', 'conflicts')
      .map(normalizeClarificationConflictForUi)
      .filter((conflict): conflict is StrategistClarificationConflict => conflict !== null),
  };
}

function isReasoningTraceStep(step: StrategistTraceStep) {
  return step.kind !== 'tool';
}

function StrategistTraceTimeline({
  trace,
  turnId,
}: {
  trace: StrategistTrace;
  turnId: string;
}) {
  const steps = trace.steps.filter(step => !['Prepared the response', 'Completed the request'].includes(step.title));
  if (!steps.length) return null;
  return (
    <details className="strategist-trace-panel" aria-label="Steps taken">
      <summary>
        <span>{steps.length} workspace steps</span>
      </summary>
      <ol className="strategist-trace-list">
        {steps.map((step, index) => {
          const isThought = isReasoningTraceStep(step);
          return (
            <li
              key={`${turnId}-trace-${index}`}
              className={isThought ? 'is-thought' : 'is-tool'}
            >
              <details className="strategist-trace-step">
                <summary>
                  <span className="strategist-trace-step-label">
                    {formatWorkspaceCopy(step.title)}
                  </span>
                  {typeof step.durationMs === 'number' && (
                    <span className="strategist-trace-duration">
                      {step.durationMs >= 1000
                        ? `${(step.durationMs / 1000).toFixed(1)}s`
                        : `${Math.round(step.durationMs)}ms`}
                    </span>
                  )}
                </summary>
                <div className="strategist-trace-step-detail">
                  <p>{formatWorkspaceCopy(step.detail)}</p>
                </div>
              </details>
            </li>
          );
        })}
      </ol>
    </details>
  );
}

function isReasoningActivity(activity: StrategistRunActivity) {
  return activity.kind === 'understanding'
    || activity.kind === 'context'
    || activity.kind === 'planning';
}

function normalizeTaskReferenceForUi(value: unknown): StrategistTaskReference | null {
  if (!isRecord(value)) return null;
  const taskId = value.taskId ?? value.task_id;
  const categoryId = value.categoryId ?? value.category_id;
  const title = getStringField(value, 'title', 'title');
  if (typeof taskId !== 'number' || typeof categoryId !== 'number' || !title) {
    return null;
  }
  return {
    taskId,
    title,
    emoji: getStringField(value, 'emoji', 'emoji'),
    nodeType: getStringField(value, 'nodeType', 'node_type') === 'habit' ? 'habit' : 'task',
    categoryId,
    categoryName: getStringField(value, 'categoryName', 'category_name'),
    categoryAccentColor: getStringField(
      value,
      'categoryAccentColor',
      'category_accent_color',
    ),
  };
}

function formatDuration(minutes: number) {
  if (minutes < 60) return `${minutes} minute${minutes === 1 ? '' : 's'}`;
  const hours = Math.floor(minutes / 60);
  const remainder = minutes % 60;
  return remainder === 0
    ? `${hours} hour${hours === 1 ? '' : 's'}`
    : `${hours}h ${remainder}m`;
}

function getDistinctClarificationFollowUps(
  clarification: StrategistClarificationPayload,
) {
  const structuredChoiceKeys = new Set(
    [...clarification.entityChoices, ...clarification.fieldChoices]
      .flatMap((choice) => [choice.label, choice.followUpPrompt ?? ''])
      .map((value) => value.trim().toLocaleLowerCase())
      .filter(Boolean),
  );
  const seen = new Set<string>();
  return clarification.scopedFollowUpPrompts.filter((prompt) => {
    const key = prompt.trim().toLocaleLowerCase();
    if (!key || structuredChoiceKeys.has(key) || seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function normalizeStrategistResponseForUi(
  response: StrategistResponse | null | undefined,
): StrategistResponse | null {
  if (!isRecord(response)) {
    return null;
  }
  const modeValue = getStringField(response, 'mode', 'mode');
  const mode: StrategistResponse['mode'] =
    modeValue === 'metadata'
      || modeValue === 'branch'
      || modeValue === 'action'
      || modeValue === 'schedule_patch'
      || modeValue === 'clarification'
      ? modeValue
      : 'review';
  const traceValue = getObjectField<Record<string, unknown>>(response, 'trace', 'trace');
  const traceSteps = traceValue
    ? getArrayField(traceValue, 'steps', 'steps')
        .map(normalizeTraceStepForUi)
        .filter((step): step is StrategistTraceStep => step !== null)
    : [];
  const normalizedTrace =
    traceValue && traceSteps.length > 0
      ? {
          summary: getStringField(traceValue, 'summary', 'summary') ?? 'Trace',
          steps: traceSteps,
        }
      : null;
  const rawProposal = getObjectField<StrategistStandardProposal>(response, 'proposal', 'proposal');
  const normalizedScheduleProposal = rawProposal?.proposalKind === 'schedule_patch'
    ? normalizePlannerScheduleProposal(rawProposal as unknown as Record<string, unknown>)
    : null;
  const proposal = normalizedScheduleProposal
    && normalizedScheduleProposal.schedulePatchValidation.operationCount <= 0
    ? null
    : rawProposal;
  return {
    ...(response as StrategistResponse),
    providerLabel: getStringField(response, 'providerLabel', 'provider_label') ?? 'Strategist',
    reasoningSummary: getStringField(response, 'reasoningSummary', 'reasoning_summary'),
    mode,
    summary:
      getStringField(response, 'summary', 'summary') ??
      'Strategist returned a response that could not be rendered fully.',
    contextCards: getArrayField(response, 'contextCards', 'context_cards'),
    suggestedPrompts: getArrayField<string>(response, 'suggestedPrompts', 'suggested_prompts')
      .filter((item) => typeof item === 'string' && item.trim().length > 0),
    taskReferences: getArrayField(response, 'taskReferences', 'task_references')
      .map(normalizeTaskReferenceForUi)
      .filter((item): item is StrategistTaskReference => item !== null),
    proposal,
    clarification: normalizeClarificationForUi(response.clarification),
    trace: normalizedTrace,
    reviewDocument: normalizeReviewDocumentForUi(
      response.reviewDocument ?? response.review_document,
    ),
  };
}

function mapTurnRecordToChatTurn(turn: StrategistTurnRecord, isHistorical: boolean): StrategistChatTurn {
  return {
    id: turn.id,
    prompt: turn.prompt,
    ...(turn.response?.reasoningSummary ? { reasoning: turn.response.reasoningSummary } : {}),
    response: normalizeStrategistResponseForUi(turn.response),
    error: turn.error ?? null,
    actionStatus: 'idle',
    actionError: null,
    reviewActionId: null,
    reviewOutcome: null,
    isHistorical,
    createdAt: turn.createdAt,
  };
}

function mergeSessionSummary(
  current: StrategistSessionSummary[],
  nextSession: StrategistSessionSummary,
) {
  const next = [nextSession, ...current.filter((item) => item.id !== nextSession.id)];
  return next.sort(
    (a, b) => new Date(b.updatedAt).getTime() - new Date(a.updatedAt).getTime(),
  );
}

function isIntegerId(value: unknown): value is number {
  return typeof value === 'number' && Number.isInteger(value);
}

function getStandardProposalFallbackHeading(proposal: StrategistStandardProposal) {
  if (proposal.proposalKind === 'action') {
    return 'Impact preview';
  }
  if (proposal.proposalKind === 'branch') {
    if (proposal.operation === 'mixed') {
      return 'Combined changes';
    }
    if (['move', 'reorder', 'restructure'].includes(proposal.operation)) {
      return 'Tree changes';
    }
    return proposal.operation === 'create' ? 'Planned additions' : 'Task-tree preview';
  }
  return 'Changes';
}

function getTaskPatchOperationCount(proposal: StrategistStandardProposal) {
  const simulationOperationCount = proposal.taskPatchSimulation?.['operation_count'];
  const validationOperationCount = proposal.taskPatchValidation?.['operation_count'];
  if (typeof simulationOperationCount === 'number' && Number.isFinite(simulationOperationCount)) {
    return simulationOperationCount;
  }
  if (typeof validationOperationCount === 'number' && Number.isFinite(validationOperationCount)) {
    return validationOperationCount;
  }

  const rawProposal = proposal as unknown as Record<string, unknown>;
  const taskPatch = isRecord(proposal.taskPatch)
    ? proposal.taskPatch
    : isRecord(rawProposal.task_patch)
      ? rawProposal.task_patch
      : null;
  const operations = taskPatch?.operations;
  return Array.isArray(operations) ? operations.length : null;
}

function getPreferencePatchOperationCount(proposal: StrategistStandardProposal) {
  const simulationOperationCount =
    proposal.preferencePatchSimulation?.['operationCount']
    ?? proposal.preferencePatchSimulation?.['operation_count'];
  const validationOperationCount =
    proposal.preferencePatchValidation?.['operationCount']
    ?? proposal.preferencePatchValidation?.['operation_count'];
  if (typeof simulationOperationCount === 'number' && Number.isFinite(simulationOperationCount)) {
    return simulationOperationCount;
  }
  if (typeof validationOperationCount === 'number' && Number.isFinite(validationOperationCount)) {
    return validationOperationCount;
  }

  const rawProposal = proposal as unknown as Record<string, unknown>;
  const preferencePatch = isRecord(proposal.preferencePatch)
    ? proposal.preferencePatch
    : isRecord(rawProposal.preference_patch)
      ? rawProposal.preference_patch
      : null;
  const operations = preferencePatch?.operations;
  return Array.isArray(operations) ? operations.length : null;
}

function getPreferenceRule(proposal: StrategistStandardProposal) {
  const rawProposal = proposal as unknown as Record<string, unknown>;
  const preferencePatch = isRecord(proposal.preferencePatch)
    ? proposal.preferencePatch
    : isRecord(rawProposal.preference_patch)
      ? rawProposal.preference_patch
      : null;
  const operations = Array.isArray(preferencePatch?.operations)
    ? preferencePatch.operations.filter(isRecord)
    : [];
  const constraintOperation = operations.find(
    (operation) => operation.op === 'create_constraint' && isRecord(operation.constraint),
  );
  const preferenceOperation = operations.find(
    (operation) => operation.op === 'create_preference' && isRecord(operation.preference),
  );
  const constraint = constraintOperation && isRecord(constraintOperation.constraint)
    ? constraintOperation.constraint
    : null;
  const preference = preferenceOperation && isRecord(preferenceOperation.preference)
    ? preferenceOperation.preference
    : null;
  const description = constraint?.description ?? preference?.description ?? proposal.preview[0];
  return typeof description === 'string' && description.trim()
    ? formatWorkspaceCopy(description.trim())
    : 'Use this preference when planning my schedule.';
}

function canApproveStandardProposal(proposal: StrategistStandardProposal | null) {
  if (!proposal?.proposalId) {
    return false;
  }
  if (proposal.proposalKind === 'schedule_patch') {
    if (!['valid', 'warning'].includes(proposal.schedulePatchStatus ?? 'invalid')) {
      return false;
    }
    if (proposal.taskPatchStatus === 'invalid') {
      return false;
    }
    const normalized = normalizePlannerScheduleProposal(
      proposal as unknown as Record<string, unknown>,
    );
    return (normalized?.schedulePatchValidation.operationCount ?? 0) > 0;
  }
  if (proposal.proposalKind === 'preference_memory') {
    if (proposal.preferencePatchStatus === 'invalid') {
      return false;
    }
    const operationCount = getPreferencePatchOperationCount(proposal);
    return operationCount !== null && operationCount > 0;
  }
  if (proposal.taskPatchStatus === 'invalid') {
    return false;
  }
  const operationCount = getTaskPatchOperationCount(proposal);
  return operationCount !== null && operationCount > 0;
}

function getScheduleProposalLifecycle(proposal: StrategistStandardProposal | null) {
  const lifecycle = proposal?.payload?.schedulePatchLifecycle;
  return isRecord(lifecycle) && typeof lifecycle.status === 'string'
    ? lifecycle.status
    : 'pending';
}

function getScheduleConflicts(proposal: StrategistStandardProposal | null) {
  if (proposal?.proposalKind !== 'schedule_patch') {
    return [];
  }
  return normalizePlannerScheduleProposal(
    proposal as unknown as Record<string, unknown>,
  )?.schedulePatchSimulation.conflicts ?? [];
}

function getStandardProposalTargetNodeIds(proposal: StrategistStandardProposal): number[] {
  const ids = Array.isArray(proposal.targetNodeIds)
    ? proposal.targetNodeIds.filter(isIntegerId)
    : [];
  if (ids.length > 0) {
    return ids;
  }
  return isIntegerId(proposal.targetNodeId) ? [proposal.targetNodeId] : [];
}

function renderStandardProposalSupportContent(
  proposal: StrategistStandardProposal,
  taskReferences: AiTaskReference[],
) {
  if (proposal.proposalKind === 'preference_memory') {
    return null;
  }
  if (proposal.proposalKind === 'schedule_patch') {
    const schedule = normalizePlannerScheduleProposal(
      proposal as unknown as Record<string, unknown>,
    );
    const blocks = schedule?.schedulePatchSimulation.previewBlocks ?? [];
    const removals = (schedule?.schedulePatch.operations ?? []).filter(
      (operation) => operation.op === 'remove_block',
    );
    const taskPatch = isRecord(proposal.taskPatch) ? proposal.taskPatch : null;
    const taskOperations = Array.isArray(taskPatch?.operations)
      ? taskPatch.operations.filter(isRecord)
      : [];
    const estimateChanges = taskOperations
      .map((operation) => isRecord(operation.fields)
        ? operation.fields.time_estimate_minutes
        : null)
      .filter((value): value is number => typeof value === 'number' && value > 0);
    if (blocks.length === 0 && removals.length === 0) return null;
    return (
      <div className="strategist-schedule-preview">
        {estimateChanges.length > 0 && (
          <div className="strategist-recurring-summary">
            {estimateChanges.map((minutes, index) => (
              <span key={`${minutes}-${index}`}>
                <strong>Task estimate:</strong> {formatDuration(minutes)}
              </span>
            ))}
          </div>
        )}
        {blocks.length > 0 && (
          <>
            <p className="strategist-clarification-label">Proposed times</p>
            <ul className="strategist-schedule-preview-list" aria-label="Proposed schedule">
              {blocks.map((block) => (
                <li key={block.id}>
                  <span className="strategist-schedule-preview-task">
                    {block.emoji && <span aria-hidden="true">{block.emoji}</span>}
                    <strong>{formatWorkspaceCopy(block.title)}</strong>
                  </span>
                  <time>{formatSessionTime(block.startAt)} – {formatSessionTime(block.endAt)}</time>
                </li>
              ))}
            </ul>
          </>
        )}
        {removals.length > 0 && (
          <>
            <p className="strategist-clarification-label">Will be cancelled</p>
            <ul className="strategist-schedule-preview-list" aria-label="Calendar items to cancel">
              {removals.map((operation, index) => (
                <li key={`remove-${operation.acceptedBlockId ?? operation.taskId ?? index}`}>
                  <span className="strategist-schedule-preview-task">
                    {operation.emoji && <span aria-hidden="true">{operation.emoji}</span>}
                    <strong>{formatWorkspaceCopy(operation.title ?? 'Scheduled item')}</strong>
                  </span>
                  {operation.startAt && operation.endAt ? (
                    <time>
                      {formatSessionTime(operation.startAt)} – {formatSessionTime(operation.endAt)}
                    </time>
                  ) : (
                    <span>Scheduled calendar item</span>
                  )}
                </li>
              ))}
            </ul>
          </>
        )}
      </div>
    );
  }
  const createdTaskPlan = isRecord(proposal.payload?.created_task_schedule_plan)
    ? proposal.payload.created_task_schedule_plan
    : null;
  if (createdTaskPlan) {
    const blocks = Array.isArray(createdTaskPlan.blocks)
      ? createdTaskPlan.blocks.filter(isRecord)
      : [];
    const splitPlan = Array.isArray(createdTaskPlan.split_session_plan)
      ? createdTaskPlan.split_session_plan.filter(isRecord)
      : [];
    const conflictResolutions = Array.isArray(createdTaskPlan.conflict_resolutions)
      ? createdTaskPlan.conflict_resolutions.filter(isRecord)
      : [];
    const movedSchedules = compactMovedSchedules(conflictResolutions);
    return (
      <div className="strategist-schedule-preview">
        <p className="strategist-clarification-label">Proposed times</p>
        <ul className="strategist-schedule-preview-list" aria-label="Proposed new task schedule">
          {blocks.map((block, index) => {
            const startAt = typeof block.start_at === 'string' ? block.start_at : '';
            const endAt = typeof block.end_at === 'string' ? block.end_at : '';
            const taskTitle = typeof block.task_title === 'string' ? block.task_title : 'Task';
            return (
              <li key={`${String(block.target_temp_id ?? taskTitle)}-${startAt}-${index}`}>
                <strong>
                  <RichAiText text={taskTitle} taskReferences={taskReferences} />
                </strong>
                <time>{formatSessionTime(startAt)} – {formatSessionTime(endAt)}</time>
              </li>
            );
          })}
        </ul>
        {splitPlan.length > 0 && (
          <small>Long work is split around the requested break while preserving its full estimate.</small>
        )}
        {movedSchedules.length > 0 && (
          <div className="strategist-clarification-group">
            <p className="strategist-clarification-label">Making room</p>
            <ul className="strategist-action-list strategist-compact-recurrence-list">
              {movedSchedules.map((moved) => (
                <li key={`${moved.identity}-${moved.title}-${moved.originalStartAt}-${moved.movedStartAt}`}>
                  <strong>
                    <RichAiText text={moved.title} taskReferences={taskReferences} />
                  </strong>
                  <span>{moved.pattern} · {moved.period}</span>
                  <small>
                    {formatClockOnly(moved.originalStartAt)}–{formatClockOnly(moved.originalEndAt)}
                    {' → '}
                    {formatClockOnly(moved.movedStartAt)}–{formatClockOnly(moved.movedEndAt)}
                  </small>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    );
  }
  const recurringPlan = isRecord(proposal.payload?.recurring_schedule_plan)
    ? proposal.payload.recurring_schedule_plan
    : null;
  if (recurringPlan) {
    const occurrences = Array.isArray(recurringPlan.occurrences)
      ? recurringPlan.occurrences.filter(isRecord)
      : [];
    const categoryName = typeof recurringPlan.category_name === 'string'
      ? recurringPlan.category_name
      : null;
    const durationMinutes = typeof recurringPlan.duration_minutes === 'number'
      ? recurringPlan.duration_minutes
      : null;
    const conflictResolutions = Array.isArray(recurringPlan.conflict_resolutions)
      ? recurringPlan.conflict_resolutions.filter(isRecord)
      : [];
    const movedSchedules = compactMovedSchedules(conflictResolutions);
    const recurrenceDays = Array.isArray(recurringPlan.days_of_week)
      ? recurringPlan.days_of_week.filter((value): value is string => typeof value === 'string')
      : [];
    const frequency = typeof recurringPlan.frequency === 'string'
      ? recurringPlan.frequency.trim().toLocaleLowerCase()
      : 'daily';
    const recurrenceLabel = frequency === 'weekly'
      ? weekdayPatternFromNames(recurrenceDays) || 'Every week'
      : frequency === 'daily'
        ? 'Every day'
        : `Repeats ${frequency.replaceAll('_', ' ')}`;
    const firstOccurrence = occurrences[0];
    const firstStartAt = typeof firstOccurrence?.start_at === 'string'
      ? firstOccurrence.start_at
      : '';
    const firstEndAt = typeof firstOccurrence?.end_at === 'string'
      ? firstOccurrence.end_at
      : '';
    const scheduleStart = typeof recurringPlan.start_date === 'string'
      ? recurringPlan.start_date
      : firstStartAt;
    const scheduleEnd = typeof recurringPlan.series_end_date === 'string'
      ? recurringPlan.series_end_date
      : typeof recurringPlan.end_date === 'string'
        ? recurringPlan.end_date
        : typeof occurrences.at(-1)?.end_at === 'string'
          ? occurrences.at(-1)?.end_at as string
          : scheduleStart;
    const schedulePeriod = formatSchedulePeriod(
      scheduleStart,
      scheduleEnd,
      recurringPlan.end_mode === 'never',
    );
    const firstClock = `${formatClockOnly(firstStartAt)}-${formatClockOnly(firstEndAt)}`;
    const meaningfulOccurrences = occurrences.filter((occurrence, index) => {
      if (occurrence.shifted === true) return true;
      if (index === 0) return false;
      const startAt = typeof occurrence.start_at === 'string' ? occurrence.start_at : '';
      const endAt = typeof occurrence.end_at === 'string' ? occurrence.end_at : '';
      return `${formatClockOnly(startAt)}-${formatClockOnly(endAt)}` !== firstClock;
    });
    const shownExceptions = meaningfulOccurrences.slice(0, 3);
    return (
      <div className="strategist-schedule-preview">
        <div className="strategist-recurring-summary">
          {categoryName && <span><strong>Workspace:</strong> {categoryName}</span>}
          {durationMinutes != null && (
            <span><strong>Duration:</strong> {formatDuration(durationMinutes)}</span>
          )}
          <span><strong>Repeats:</strong> {recurrenceLabel}</span>
        </div>
        {movedSchedules.length > 0 && (
          <div className="strategist-clarification-group">
            <p className="strategist-clarification-label">Making room</p>
            <ul className="strategist-action-list strategist-compact-recurrence-list">
              {movedSchedules.map((moved) => (
                <li key={`${moved.identity}-${moved.title}-${moved.originalStartAt}-${moved.movedStartAt}`}>
                  <strong>
                    <RichAiText text={moved.title} taskReferences={taskReferences} />
                  </strong>
                  <span>{moved.pattern} · {moved.period}</span>
                  <small>
                    {formatClockOnly(moved.originalStartAt)}–{formatClockOnly(moved.originalEndAt)}
                    {' → '}
                    {formatClockOnly(moved.movedStartAt)}–{formatClockOnly(moved.movedEndAt)}
                  </small>
                </li>
              ))}
            </ul>
          </div>
        )}
        <p className="strategist-clarification-label">Schedule</p>
        <div className="strategist-compact-recurrence-card" aria-label="Proposed recurring schedule">
          <strong>{recurrenceLabel}</strong>
          {firstStartAt && firstEndAt && (
            <span>{formatClockOnly(firstStartAt)}–{formatClockOnly(firstEndAt)}</span>
          )}
          {schedulePeriod && <small>{schedulePeriod}</small>}
          {shownExceptions.length > 0 ? (
            <ul aria-label="Schedule exceptions">
              {shownExceptions.map((occurrence) => {
                const startAt = typeof occurrence.start_at === 'string' ? occurrence.start_at : '';
                const endAt = typeof occurrence.end_at === 'string' ? occurrence.end_at : '';
                const occurrenceId = typeof occurrence.occurrence_id === 'string'
                  ? occurrence.occurrence_id
                  : `${startAt}-${endAt}`;
                return (
                  <li key={occurrenceId}>
                    {formatShortDate(startAt, true)} at {formatClockOnly(startAt)}
                    {endAt ? `â€“${formatClockOnly(endAt)}` : ''}
                    {occurrence.shifted === true ? ' Â· adjusted around another event' : ' Â· different time'}
                  </li>
                );
              })}
              {meaningfulOccurrences.length > shownExceptions.length ? (
                <li>{meaningfulOccurrences.length - shownExceptions.length} more schedule exceptions</li>
              ) : null}
            </ul>
          ) : null}
        </div>
      </div>
    );
  }
  const rawProposal = proposal as unknown as Record<string, unknown>;
  const reviewSections = getArrayField(rawProposal, 'reviewSections', 'review_sections')
    .filter(isRecord)
    .map((section) => ({
      title: getStringField(section, 'title', 'title') ?? 'Changes',
      items: getArrayField(section, 'items', 'items')
        .filter(isRecord)
        .map((item) => ({
          text: getStringField(item, 'text', 'text'),
          taskIds: getArrayField(item, 'taskIds', 'task_ids').filter(isIntegerId),
        }))
        .filter((item): item is { text: string; taskIds: number[] } => Boolean(item.text)),
    }))
    .filter((section) => section.items.length > 0);
  if (reviewSections.length > 0) {
    return (
      <>
        {proposal.validationWarnings.length > 0 && (
          <div className="strategist-clarification-group">
            <p className="strategist-clarification-label">What to know</p>
            <ul className="strategist-action-list">
              {proposal.validationWarnings.map((item) => (
                <li key={item}>{formatWorkspaceCopy(item)}</li>
              ))}
            </ul>
          </div>
        )}
        {reviewSections.map((section) => (
          <div key={section.title} className="strategist-clarification-group">
            <p className="strategist-clarification-label">
              {formatWorkspaceCopy(section.title)}
            </p>
            <ul className="strategist-action-list">
              {section.items.map((item, index) => (
                <li key={`${section.title}-${item.text}-${index}`}>
                  <RichAiText
                    text={formatWorkspaceCopy(item.text)}
                    taskReferences={taskReferences.filter((reference) => (
                      item.taskIds.includes(Number(reference.node.id))
                    ))}
                  />
                </li>
              ))}
            </ul>
          </div>
        ))}
      </>
    );
  }
  const scheduleValidation = isRecord(proposal.schedulePatchValidation)
    ? proposal.schedulePatchValidation
    : null;
  const scheduleValidationDetails = scheduleValidation
    ? [scheduleValidation.errors, scheduleValidation.warnings]
        .flatMap((items) => Array.isArray(items) ? items : [])
        .filter((item): item is string => typeof item === 'string' && item.trim().length > 0)
    : [];
  const rawTaskPatch = isRecord(proposal.taskPatch) ? proposal.taskPatch : null;
  const taskOperations = Array.isArray(rawTaskPatch?.operations)
    ? rawTaskPatch.operations.filter(isRecord)
    : [];
  const moveOperations = taskOperations.filter((operation) => operation.op === 'move');
  const archiveOperations = taskOperations.filter(
    (operation) => operation.op === 'delete' && operation.mode === 'archive',
  );
  const deleteOperations = taskOperations.filter(
    (operation) => operation.op === 'delete' && operation.mode !== 'archive',
  );
  const isBatchCleanup = proposal.proposalKind === 'branch'
    && taskOperations.length > 1
    && (moveOperations.length + archiveOperations.length + deleteOperations.length > 0);
  const moveGroups = new Map<string, string[]>();
  moveOperations.forEach((operation) => {
    const destination = getStringField(
      operation,
      'category_name',
      'destination_parent_title',
    ) ?? 'New location';
    const title = getStringField(operation, 'node_title', 'task_title') ?? 'Task';
    moveGroups.set(destination, [...(moveGroups.get(destination) ?? []), title]);
  });
  const archiveTitles = archiveOperations.map(
    (operation) => getStringField(operation, 'node_title', 'task_title') ?? 'Task',
  );
  const deleteTitles = deleteOperations.map(
    (operation) => getStringField(operation, 'node_title', 'task_title') ?? 'Task',
  );
  return (
    <>
      {proposal.validationWarnings.length > 0 && (
        <div className="strategist-clarification-group">
          <p className="strategist-clarification-label">What to know</p>
          <ul className="strategist-action-list">
            {proposal.validationWarnings.map((item) => (
              <li key={item}>{formatWorkspaceCopy(item)}</li>
            ))}
          </ul>
        </div>
      )}

      {scheduleValidationDetails.length > 0 && (
        <div className="strategist-clarification-group">
          <p className="strategist-clarification-label">Schedule validation</p>
          <ul className="strategist-action-list">
            {scheduleValidationDetails.map((item) => (
              <li key={item}>{formatWorkspaceCopy(item)}</li>
            ))}
          </ul>
        </div>
      )}

      {isBatchCleanup && moveGroups.size > 0 && (
        <div className="strategist-clarification-group">
          <p className="strategist-clarification-label">Move to</p>
          <ul className="strategist-action-list">
            {[...moveGroups.entries()].map(([destination, titles]) => (
              <li key={destination}>
                <strong>{formatWorkspaceCopy(destination)}:</strong>{' '}
                {titles.map((title, index) => (
                  <span key={`${title}-${index}`}>
                    {index > 0 ? ', ' : ''}
                    <RichAiText
                      text={formatWorkspaceCopy(title)}
                      taskReferences={taskReferences}
                    />
                  </span>
                ))}
              </li>
            ))}
          </ul>
        </div>
      )}

      {isBatchCleanup && archiveTitles.length > 0 && (
        <div className="strategist-clarification-group">
          <p className="strategist-clarification-label">Archive</p>
          <ul className="strategist-action-list">
            {archiveTitles.map((title, index) => (
              <li key={`${title}-${index}`}>
                <RichAiText text={formatWorkspaceCopy(title)} taskReferences={taskReferences} />
              </li>
            ))}
          </ul>
        </div>
      )}

      {isBatchCleanup && deleteTitles.length > 0 && (
        <div className="strategist-clarification-group">
          <p className="strategist-clarification-label">Delete</p>
          <ul className="strategist-action-list">
            {deleteTitles.map((title, index) => (
              <li key={`${title}-${index}`}>
                <RichAiText text={formatWorkspaceCopy(title)} taskReferences={taskReferences} />
              </li>
            ))}
          </ul>
        </div>
      )}

      {!isBatchCleanup && proposal.preview.length > 0 && (
        <div className="strategist-clarification-group">
          <p className="strategist-clarification-label">
            {getStandardProposalFallbackHeading(proposal)}
          </p>
          <ul className="strategist-action-list">
            {proposal.preview.map((item) => (
              <li key={item}>
                <RichAiText text={formatWorkspaceCopy(item)} taskReferences={taskReferences} />
              </li>
            ))}
          </ul>
        </div>
      )}

    </>
  );
}

function getRevisedSourceProposalId(response: StrategistResponse | null | undefined) {
  const actionResult = response?.actionResult;
  if (
    actionResult?.status === 'revised'
    && typeof actionResult.sourceProposalId === 'string'
    && actionResult.sourceProposalId.trim()
  ) {
    return actionResult.sourceProposalId.trim();
  }

  const payload = response?.proposal?.payload;
  if (!isRecord(payload)) {
    return null;
  }
  return getStringField(payload, 'revisionOfProposalId', 'revision_of_proposal_id');
}

function markReplacedProposalTurns(
  turns: StrategistChatTurn[],
  replacedProposalIds: Set<string>,
) {
  if (replacedProposalIds.size === 0) {
    return turns;
  }
  return turns.map((turn) => {
    const proposalId = turn.response?.proposal?.proposalId;
    return typeof proposalId === 'string' && replacedProposalIds.has(proposalId)
      ? { ...turn, actionStatus: 'revised' as const, actionError: null }
      : turn;
  });
}

function renderDecisionSummary(summary: string, taskReferences: AiTaskReference[]) {
  return <section className="strategist-decision-panel" aria-label="AI response"><ChatMarkdown text={summary} taskReferences={taskReferences} /></section>;
}

function isBadgeSuggestion(value: unknown): value is StrategistBadgeSuggestion {
  if (!value || typeof value !== 'object') {
    return false;
  }
  const maybeSuggestion = value as Record<string, unknown>;
  return (
    typeof maybeSuggestion.emoji === 'string' ||
    typeof maybeSuggestion.label === 'string' ||
    typeof maybeSuggestion.targetNodeTitle === 'string' ||
    typeof maybeSuggestion.target_node_title === 'string'
  );
}

function getStringField(
  value: Record<string, unknown>,
  camelKey: string,
  snakeKey: string,
) {
  const camelValue = value[camelKey];
  if (typeof camelValue === 'string' && camelValue.trim()) {
    return camelValue.trim();
  }
  const snakeValue = value[snakeKey];
  if (typeof snakeValue === 'string' && snakeValue.trim()) {
    return snakeValue.trim();
  }
  return null;
}

function normalizeBadgeColor(value?: string | null) {
  if (!value || !/^#[0-9a-f]{3,8}$/i.test(value.trim())) {
    return null;
  }
  return value.trim();
}

function getBadgeSuggestions(proposal: StrategistStandardProposal) {
  const explicitSuggestions = proposal.badgeSuggestions;
  const payloadSuggestions =
    proposal.payload?.badgeSuggestions ?? proposal.payload?.badge_suggestions;
  const rawSuggestions = Array.isArray(explicitSuggestions)
    ? explicitSuggestions
    : Array.isArray(payloadSuggestions)
      ? payloadSuggestions
      : [];

  return rawSuggestions.filter(isBadgeSuggestion).slice(0, 4);
}

function renderBadgeSuggestions(proposal: StrategistStandardProposal) {
  const suggestions = getBadgeSuggestions(proposal);
  if (suggestions.length === 0) {
    return null;
  }

  return (
    <div className="strategist-badge-preview" aria-label="Review-only badge suggestions">
      <div>
        <p className="strategist-clarification-label">Badge suggestions</p>
        <p>
          Preview only. Badges cannot be changed until a future change operation supports them.
        </p>
      </div>
      <div className="strategist-badge-preview-list">
        {suggestions.map((suggestion, index) => {
          const suggestionRecord = suggestion as Record<string, unknown>;
          const title =
            getStringField(suggestionRecord, 'targetNodeTitle', 'target_node_title') ??
            suggestion.label ??
            `Suggestion ${index + 1}`;
          const label = suggestion.label ?? title;
          const color = normalizeBadgeColor(suggestion.color);
          return (
            <div key={`${title}-${suggestion.emoji ?? index}`} className="strategist-badge-chip">
              <span
                className="strategist-badge-dot"
                style={color ? { backgroundColor: color } : undefined}
                aria-hidden="true"
              >
                {suggestion.emoji ?? '*'}
              </span>
              <div>
                <strong>{label}</strong>
                <small>{title}</small>
                {suggestion.rationale && <p>{formatWorkspaceCopy(suggestion.rationale)}</p>}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function NewChatIcon() {
  return (
    <svg viewBox="0 0 20 20" aria-hidden="true" focusable="false">
      <path d="M4.5 15.5h10.8" />
      <path d="M5.2 13.5 13.7 5a1.8 1.8 0 0 1 2.6 2.6l-8.5 8.5-3.3.7.7-3.3Z" />
    </svg>
  );
}

function HistoryIcon() {
  return (
    <svg viewBox="0 0 20 20" aria-hidden="true" focusable="false">
      <path d="M5 15.2 3.8 18l3.1-1a7.2 7.2 0 1 0-2.7-2.7Z" />
      <path d="M7.4 8.4h5.2" />
      <path d="M7.4 11.2h3.7" />
    </svg>
  );
}

function ExpandIcon({ expanded }: { expanded: boolean }) {
  if (expanded) {
    return (
      <svg viewBox="0 0 20 20" aria-hidden="true" focusable="false">
        <path d="M8 4.5v3.5H4.5" />
        <path d="m4.5 8 4-4" />
        <path d="M12 15.5V12h3.5" />
        <path d="m15.5 12-4 4" />
      </svg>
    );
  }

  return (
    <svg viewBox="0 0 20 20" aria-hidden="true" focusable="false">
      <path d="M7.8 4.2H4.2v3.6" />
      <path d="m4.2 4.2 4.2 4.2" />
      <path d="M12.2 15.8h3.6v-3.6" />
      <path d="m15.8 15.8-4.2-4.2" />
    </svg>
  );
}

function CloseIcon() {
  return (
    <svg viewBox="0 0 20 20" aria-hidden="true" focusable="false">
      <path d="m5.5 5.5 9 9" />
      <path d="m14.5 5.5-9 9" />
    </svg>
  );
}

export function StrategistAiCard({
  embedded = false,
  activeCategory,
  currentTask,
  stagedNodeIds,
  isExpanded,
  onToggleExpanded,
  onClose,
  launchRequest,
  onLaunchHandled,
  onRunUpdate,
  onSessionActivity,
}: StrategistAiCardProps) {
  const [turns, setTurns] = useState<StrategistChatTurn[]>([]);
  const [sessions, setSessions] = useState<StrategistSessionSummary[]>([]);
  const [activeSession, setActiveSession] = useState<StrategistSessionSummary | null>(null);
  const [isHistoryOpen, setIsHistoryOpen] = useState(false);
  const [historyQuery, setHistoryQuery] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const [activityLabel, setActivityLabel] = useState('Strategist is thinking…');
  const [runActivities, setRunActivities] = useState<StrategistRunActivity[]>([]);
  const [isLoadingHistory, setIsLoadingHistory] = useState(true);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const fetchNodes = useNodeStore((state) => state.fetchNodes);
  const fetchAllNodes = useNodeStore((state) => state.fetchAllNodes);
  const categoryNodeCache = useNodeStore((state) => state.categoryNodeCache);
  const categories = useCategoryStore((state) => state.categories);
  const calendarRange = useCalendarStore((state) => state.lastRange);
  const ingestSharedScheduleProposal = useCalendarStore(
    (state) => state.ingestSharedScheduleProposal,
  );
  const rejectSharedScheduleProposal = useCalendarStore(
    (state) => state.rejectSchedulePatchProposal,
  );
  const markSchedulePatchProposalApplied = useCalendarStore(
    (state) => state.markSchedulePatchProposalApplied,
  );
  const fetchCalendarBlocks = useCalendarStore((state) => state.fetchBlocks);
  const activeScheduleProposal = useCalendarStore(
    (state) => state.schedulePatchProposal?.proposal ?? null,
  );
  const surfaceRef = useRef<HTMLElement | null>(null);
  const requestControllerRef = useRef<AbortController | null>(null);
  const activeTurnIdRef = useRef<string | null>(null);
  const promptInputRef = useRef<HTMLTextAreaElement | null>(null);
  const stagedNodeIdsRef = useRef(stagedNodeIds);
  const calendarRangeRef = useRef(calendarRange);
  const sharedResultGenerationRef = useRef(0);
  const historyRequestGenerationRef = useRef(0);
  const actionStatusRefs = useRef(new Map<string, HTMLDivElement>());
  const reviewActionPendingRef = useRef(false);
  const handledLaunchRequestIdsRef = useRef(new Set<string>());
  const panelProposalActionHandlerRef = useRef<((options: {
    turnId: string;
    standardProposal: StrategistStandardProposal;
    action: 'approve' | 'edit' | 'reject';
  }) => Promise<void>) | null>(null);
  const isMountedRef = useRef(true);
  const [pendingReviewActionId, setPendingReviewActionId] = useState<string | null>(null);
  useEffect(() => {
    const changed = stagedNodeIdsRef.current.join('|') !== stagedNodeIds.join('|');
    stagedNodeIdsRef.current = stagedNodeIds;
    if (changed) {
      sharedResultGenerationRef.current += 1;
    }
  }, [stagedNodeIds]);

  useEffect(() => {
    const previous = calendarRangeRef.current;
    const previousKey = previous ? `${previous.startAt}|${previous.endAt}` : '';
    const nextKey = calendarRange ? `${calendarRange.startAt}|${calendarRange.endAt}` : '';
    calendarRangeRef.current = calendarRange;
    if (previousKey !== nextKey) {
      sharedResultGenerationRef.current += 1;
    }
  }, [calendarRange]);

  useEffect(() => {
    // React StrictMode runs a development-only setup/cleanup/setup cycle.
    // Reset the ref during setup so live results are not mistaken for post-unmount responses.
    isMountedRef.current = true;
    return () => {
      isMountedRef.current = false;
      sharedResultGenerationRef.current += 1;
    };
  }, []);
  const flatNodes = useMemo(() => {
    const flatten = (nodes: StrategyNode[]): StrategyNode[] =>
      nodes.flatMap((node) => [node, ...flatten(node.children)]);
    return Object.values(categoryNodeCache).flatMap(flatten);
  }, [categoryNodeCache]);
  const categoryById = useMemo(
    () => new Map(categories.map((category) => [category.id, category])),
    [categories],
  );
  const taskReferencesByTurnId = useMemo(() => new Map(
    turns.map((turn) => [
      turn.id,
      (turn.response?.taskReferences ?? []).map((reference): AiTaskReference => ({
        node: {
          id: String(reference.taskId),
          title: reference.title,
          emoji: reference.emoji,
          type: reference.nodeType === 'habit' ? 'habit' : 'task',
        },
        category: reference.categoryName
          ? {
              name: reference.categoryName,
              accentColor: reference.categoryAccentColor ?? '#6366f1',
            }
          : null,
      })),
    ]),
  ), [turns]);
  const attachedTasks = useMemo(() => {
    const nodeById = new Map(flatNodes.map((node) => [node.id, node]));
    return stagedNodeIds.flatMap((id) => {
      const node = nodeById.get(id);
      return node ? [{ node, category: categoryById.get(node.categoryId) ?? null }] : [];
    });
  }, [categoryById, flatNodes, stagedNodeIds]);

  const latestCompletedTurn = useMemo(
    () =>
      [...turns]
        .reverse()
        .find((turn) => turn.response !== null && turn.error === null) ?? null,
    [turns],
  );
  const promptSuggestions = latestCompletedTurn?.response?.suggestedPrompts ?? [];
  const hasPendingTurn = turns.some((turn) => !turn.response && !turn.error);
  const showPromptSuggestions = promptSuggestions.length > 0
    && !latestCompletedTurn?.response?.clarification
    && !isHistoryOpen
    && !isLoading
    && !hasPendingTurn;

  useEffect(() => {
    const loadHistory = async () => {
      const requestGeneration = ++historyRequestGenerationRef.current;
      setIsLoadingHistory(true);
      try {
        const loadedSessions = await fetchStrategistSessions();
        if (requestGeneration !== historyRequestGenerationRef.current) {
          return;
        }
        setSessions(loadedSessions);
        setActiveSession(null);
        setTurns([]);
        rejectSharedScheduleProposal();
        setHistoryError(null);
      } catch (requestError) {
        if (requestGeneration !== historyRequestGenerationRef.current) {
          return;
        }
        setHistoryError(
          requestError instanceof Error
            ? requestError.message
            : 'Failed to load AI workspace history.',
        );
      } finally {
        if (requestGeneration === historyRequestGenerationRef.current) {
          setIsLoadingHistory(false);
        }
      }
    };

    void loadHistory();
  }, [rejectSharedScheduleProposal]);

  const loadSession = async (sessionId: string) => {
    const requestGeneration = ++historyRequestGenerationRef.current;
    sharedResultGenerationRef.current += 1;
    setIsLoadingHistory(true);
    try {
      const detail = await fetchStrategistSessionDetail(sessionId);
      if (requestGeneration !== historyRequestGenerationRef.current) {
        return;
      }
      rejectSharedScheduleProposal();
      setActiveSession(detail.session);
      const replacedProposalIds = new Set(
        detail.turns
          .map((turn) => getRevisedSourceProposalId(turn.response))
          .filter((proposalId): proposalId is string => Boolean(proposalId)),
      );
      setTurns(markReplacedProposalTurns(
        detail.turns.map((turn) => mapTurnRecordToChatTurn(turn, true)),
        replacedProposalIds,
      ));
      setSessions((current) => mergeSessionSummary(current, detail.session));
      setHistoryError(null);
      setIsHistoryOpen(false);
      const latestScheduleTurn = [...detail.turns].reverse().find(
        (turn) =>
          turn.response?.proposal?.proposalKind === 'schedule_patch'
          && getScheduleProposalLifecycle(turn.response.proposal) === 'pending',
      );
      const scheduleProposal = latestScheduleTurn?.response?.proposal;
      if (scheduleProposal) {
        const normalized = normalizePlannerScheduleProposal(
          scheduleProposal as unknown as Record<string, unknown>,
        );
        if (normalized) {
          ingestSharedScheduleProposal({
            providerLabel: latestScheduleTurn?.response?.providerLabel ?? 'Planner',
            mode: 'schedule_patch',
            summary: latestScheduleTurn?.response?.summary ?? normalized.body,
            contextCards: latestScheduleTurn?.response?.contextCards ?? [],
            suggestedPrompts: latestScheduleTurn?.response?.suggestedPrompts ?? [],
            proposal: normalized,
            diagnostics: { controlPlane: 'shared_workspace_history' },
          });
        }
      }
    } catch (requestError) {
      if (requestGeneration !== historyRequestGenerationRef.current) {
        return;
      }
      setHistoryError(
        requestError instanceof Error
          ? requestError.message
          : 'Failed to load that AI workspace session.',
      );
    } finally {
      if (requestGeneration === historyRequestGenerationRef.current) {
        setIsLoadingHistory(false);
      }
    }
  };

  const applyStrategistResult = (
    strategistResult: StrategistRunResponse,
    optimisticTurnId: string,
    guard?: SharedResultGuard,
  ) => {
    if (!isMountedRef.current) {
      return;
    }
    onRunUpdate?.(strategistResult);
    const nextTurn = mapTurnRecordToChatTurn(strategistResult.turn, false);
    const revisedSourceProposalId = getRevisedSourceProposalId(nextTurn.response);
    setTurns((current) => {
      const updated = current.map((turn) =>
        turn.id === optimisticTurnId || turn.id === strategistResult.turn.id
          ? { ...turn, ...nextTurn }
          : turn,
      );
      return revisedSourceProposalId
        ? markReplacedProposalTurns(updated, new Set([revisedSourceProposalId]))
        : updated;
    });
    setActiveSession(strategistResult.session);
    setRunActivities(strategistResult.run?.activities ?? []);
    setSessions((current) => mergeSessionSummary(current, strategistResult.session));
    setHistoryError(null);
    const sharedProposal = strategistResult.turn.response?.proposal;
    if (sharedProposal?.proposalKind === 'schedule_patch') {
      const currentRange = calendarRangeRef.current;
      const currentRangeKey = currentRange ? `${currentRange.startAt}|${currentRange.endAt}` : '';
      if (
        guard
        && (
          guard.generation !== sharedResultGenerationRef.current
          || guard.calendarRangeKey !== currentRangeKey
          || guard.stagedNodeIds.join('|') !== stagedNodeIdsRef.current.join('|')
        )
      ) {
        return;
      }
      const currentIds = stagedNodeIdsRef.current;
      const responseIds = sharedProposal.targetNodeIds.map(String);
      if (
        responseIds.length !== currentIds.length
        || responseIds.some((id, index) => id !== currentIds[index])
      ) {
        return;
      }
      const normalized = normalizePlannerScheduleProposal(
        sharedProposal as unknown as Record<string, unknown>,
      );
      if (normalized) {
        ingestSharedScheduleProposal({
          providerLabel: strategistResult.turn.response?.providerLabel ?? 'Planner',
          mode: 'schedule_patch',
          summary: strategistResult.turn.response?.summary ?? normalized.body,
          contextCards: strategistResult.turn.response?.contextCards ?? [],
          suggestedPrompts: strategistResult.turn.response?.suggestedPrompts ?? [],
          proposal: normalized,
          diagnostics: { controlPlane: 'shared_workspace' },
        });
      }
    }
  };

  const waitForStrategistRunCompletion = async (
    initialResult: StrategistRunResponse,
    optimisticTurnId: string,
    guard: SharedResultGuard,
  ): Promise<StrategistRunResponse> => {
    const runId = initialResult.run?.id;
    if (!runId || TERMINAL_RUN_STATUSES.has(initialResult.run?.status ?? '')) {
      return initialResult;
    }

    if (typeof EventSource !== 'undefined') {
      try {
        return await new Promise<StrategistRunResponse>((resolve, reject) => {
          const source = createStrategistRunEventSource(runId);
          let latestResult = initialResult;
          const timeout = window.setTimeout(() => {
            source.close();
            reject(new Error('AI workspace run stream timed out.'));
          }, 2 * 60 * 60 * 1000);

          const finish = (result: StrategistRunResponse) => {
            window.clearTimeout(timeout);
            source.close();
            resolve(result);
          };

          source.addEventListener('update', (event) => {
            const parsed = parseStrategistRunEvent(event as MessageEvent<string>);
            if (!parsed) {
              return;
            }
            latestResult = parsed;
            applyStrategistResult(parsed, optimisticTurnId, guard);
            const status = parsed.run?.status ?? '';
            if (TERMINAL_RUN_STATUSES.has(status) || parsed.turn.response || parsed.turn.error) {
              finish(parsed);
            }
          });

          source.addEventListener('done', (event) => {
            const parsed = parseStrategistRunEvent(event as MessageEvent<string>);
            if (parsed) {
              latestResult = parsed;
              applyStrategistResult(parsed, optimisticTurnId, guard);
            }
            finish(latestResult);
          });

          source.onerror = () => {
            window.clearTimeout(timeout);
            source.close();
            reject(new Error('AI workspace run stream disconnected.'));
          };
        });
      } catch {
        // Polling is the compatibility fallback for dev proxies or browsers that
        // cannot keep an event stream open.
      }
    }

    let latestResult = initialResult;
    for (let attempt = 0; attempt < 4800; attempt += 1) {
      await sleep(attempt < 10 ? 750 : 1500);
      latestResult = await fetchStrategistRun(runId);
      applyStrategistResult(latestResult, optimisticTurnId, guard);

      const status = latestResult.run?.status ?? '';
      if (
        TERMINAL_RUN_STATUSES.has(status)
        || latestResult.turn.response
        || latestResult.turn.error
      ) {
        return latestResult;
      }
    }

    return latestResult;
  };

  const submitPrompt = async (
    nextPrompt?: string,
    options?: {
      focusNodeId?: string | null;
      uiEvent?: StrategistUiEvent | null;
      displayPrompt?: string | null;
    },
  ) => {
    if (requestControllerRef.current) return null;
    const resolvedPrompt = (nextPrompt ?? '').trim();
    if (!resolvedPrompt && !options?.uiEvent) {
      return null;
    }
    const displayPrompt = options?.displayPrompt?.trim() || resolvedPrompt || 'Review action';
    historyRequestGenerationRef.current += 1;
    const rangeAtSubmit = calendarRangeRef.current;
    const resultGuard: SharedResultGuard = {
      generation: sharedResultGenerationRef.current + 1,
      stagedNodeIds: [...stagedNodeIdsRef.current],
      calendarRangeKey: rangeAtSubmit ? `${rangeAtSubmit.startAt}|${rangeAtSubmit.endAt}` : '',
    };
    sharedResultGenerationRef.current = resultGuard.generation;

    const optimisticTurnId = createTurnId();
    const controller = new AbortController();
    requestControllerRef.current = controller;
    activeTurnIdRef.current = optimisticTurnId;
    setIsLoading(true);
    setActivityLabel('Understanding your request…');
    setRunActivities([]);
    setPrompt('');
    setTurns((current) => [
      ...current,
      {
        id: optimisticTurnId,
        prompt: displayPrompt,
        response: null,
        error: null,
        actionStatus: 'idle',
        actionError: null,
        reviewActionId: null,
        reviewOutcome: null,
        isHistorical: false,
      },
    ]);

    try {
      const strategistResult = await requestStrategistAdvice({
        prompt: resolvedPrompt,
        sessionId: activeSession?.id ?? null,
        activeCategoryId: activeCategory?.id ?? null,
        focusNodeId: options?.focusNodeId ?? currentTask?.id ?? null,
        stagedNodeIds,
        uiEvent: options?.uiEvent ?? null,
        calendarViewStart: calendarRange?.startAt ?? null,
        calendarViewEnd: calendarRange?.endAt ?? null,
        localDateTime: (() => {
          const now = new Date();
          const offset = now.getTimezoneOffset() * 60_000;
          return new Date(now.getTime() - offset).toISOString().slice(0, 19);
        })(),
        timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone,
      }, {
        signal: controller.signal,
        onUpdate: (result) => {
          applyStrategistResult(result, optimisticTurnId, resultGuard);
          activeTurnIdRef.current = result.turn.id;
        },
        onProgress: (progress) => {
          if (!isMountedRef.current || controller.signal.aborted) return;
          if (progress.kind === 'activity') setActivityLabel(progress.text);
          else setTurns((current) => current.map((turn) =>
            turn.id === activeTurnIdRef.current
              ? { ...turn, ...(progress.kind === 'reasoning'
                  ? { reasoning: (turn.reasoning ?? '') + progress.text }
                  : { streamedAnswer: progress.text }) }
              : turn,
          ));
        },
      });

      applyStrategistResult(strategistResult, optimisticTurnId, resultGuard);
      setActivityLabel(
        strategistResult.turn.response?.mode === 'schedule_patch'
          ? 'Planner is finalizing the schedule proposal…'
          : 'Finalizing the response…',
      );
      return await waitForStrategistRunCompletion(strategistResult, optimisticTurnId, resultGuard);
    } catch (requestError) {
      setTurns((current) =>
        current.map((turn) =>
          turn.id === optimisticTurnId || turn.id === activeTurnIdRef.current
            ? {
                ...turn,
                error:
                  controller.signal.aborted
                    ? 'Response stopped.'
                    : requestError instanceof Error
                    ? requestError.message
                    : 'Failed to contact AI workspace.',
              }
            : turn,
        ),
      );
      return null;
    } finally {
      requestControllerRef.current = null;
      setIsLoading(false);
      setActivityLabel('Strategist is thinking…');
    }
  };

  const startNewChat = () => {
    historyRequestGenerationRef.current += 1;
    sharedResultGenerationRef.current += 1;
    setPrompt('');
    setTurns([]);
    setRunActivities([]);
    setActiveSession(null);
    setIsHistoryOpen(false);
    rejectSharedScheduleProposal();
  };

  const updateTurnActionState = (
    turnId: string,
    updates: Partial<Pick<
      StrategistChatTurn,
      'actionStatus' | 'actionError' | 'reviewActionId' | 'reviewOutcome'
    >>,
  ) => {
    setTurns((current) =>
      current.map((turn) => (turn.id === turnId ? { ...turn, ...updates } : turn)),
    );
  };

  const focusTurnActionStatus = (turnId: string) => {
    window.requestAnimationFrame(() => {
      window.requestAnimationFrame(() => {
        actionStatusRefs.current.get(turnId)?.focus();
      });
    });
  };

  const refreshStrategistWorkspace = async () => {
    try {
      if (!useWorkspaceStore.getState().legacy) {
        await refreshWorkspace({ force: true });
        return useWorkspaceStore.getState().error ? 'The change was saved, but the latest workspace could not be loaded.' : null;
      }
      if (activeCategory) {
        await fetchNodes(activeCategory.id);
      } else {
        await fetchAllNodes(categories.map((category) => category.id));
      }
      return useNodeStore.getState().error
        ? 'The change was saved, but the latest task view could not be loaded.'
        : null;
    } catch {
      return 'The change was saved, but the latest task view could not be loaded.';
    }
  };

  const handleReviewAction = async (turnId: string, action: StrategistReviewAction) => {
    if (isLoading || reviewActionPendingRef.current) return;
    reviewActionPendingRef.current = true;
    setPendingReviewActionId(action.eventId);
    updateTurnActionState(turnId, {
      actionStatus: 'executing',
      actionError: null,
      reviewActionId: action.eventId,
      reviewOutcome: null,
    });
    try {
      const outcome = await submitStrategistReviewAction(action.binding.sessionId, action);
      onSessionActivity?.(action.binding.sessionId, turnId);
      if (outcome.status === 'error') {
        updateTurnActionState(turnId, {
          actionStatus: outcome.error?.code === 'stale_review' ? 'stale' : 'failed',
          actionError: null,
          reviewActionId: null,
          reviewOutcome: outcome,
        });
        focusTurnActionStatus(turnId);
        return;
      }
      const resultStatus = outcome.result?.status;
      if (resultStatus === 'edit_requested') {
        setPrompt(outcome.result?.correction ?? '');
        updateTurnActionState(turnId, {
          actionStatus: 'revised',
          actionError: null,
          reviewActionId: null,
          reviewOutcome: outcome,
        });
        promptInputRef.current?.focus();
        return;
      }
      if (resultStatus === 'decision_recorded') {
        updateTurnActionState(turnId, {
          actionStatus: 'revised',
          actionError: null,
          reviewActionId: null,
          reviewOutcome: outcome,
        });
        focusTurnActionStatus(turnId);
        return;
      }
      if (resultStatus === 'rejected') {
        updateTurnActionState(turnId, {
          actionStatus: 'dismissed',
          actionError: null,
          reviewActionId: null,
          reviewOutcome: outcome,
        });
        focusTurnActionStatus(turnId);
        return;
      }
      const execution = outcome.result?.execution;
      const executionStatus = execution?.result?.status;
      if (!outcome.result || execution?.status !== 'ok' || !execution.result) {
        updateTurnActionState(turnId, {
          actionStatus: 'failed',
          actionError: null,
          reviewActionId: null,
          reviewOutcome: outcome,
        });
        focusTurnActionStatus(turnId);
        return;
      }
      let refreshError: string | null = null;
      if (executionStatus === 'applied') {
        let refreshFailed = false;
        try {
          refreshFailed = Boolean(await refreshStrategistWorkspace());
          const currentRange = calendarRangeRef.current;
          if (currentRange) {
            await fetchCalendarBlocks(currentRange.startAt, currentRange.endAt, { force: useWorkspaceStore.getState().legacy });
            refreshFailed = refreshFailed || Boolean(useCalendarStore.getState().error);
          }
        } catch {
          refreshFailed = true;
        }
        if (refreshFailed) {
          refreshError = 'Local changes are confirmed, but the latest workspace view could not be loaded. Refresh the workspace before reviewing it.';
        }
      }
      updateTurnActionState(turnId, {
        actionStatus: executionStatus === 'verification_failed' ? 'failed' : 'confirmed',
        actionError: refreshError,
        reviewActionId: null,
        reviewOutcome: outcome,
      });
      focusTurnActionStatus(turnId);
    } catch {
      const safeFailure: StrategistReviewActionOutcome = {
        status: 'error',
        error: {
          code: 'system_failure',
          summary: 'Drasil could not complete this review action safely.',
          recovery: 'retry',
        },
      };
      updateTurnActionState(turnId, {
        actionStatus: 'failed',
        actionError: null,
        reviewActionId: null,
        reviewOutcome: safeFailure,
      });
      focusTurnActionStatus(turnId);
    } finally {
      reviewActionPendingRef.current = false;
      setPendingReviewActionId(null);
    }
  };

  const handleReviewRecovery = async (
    turnId: string,
    sessionId: string,
    recovery: 'refresh' | 'revise',
  ) => {
    if (recovery === 'revise') {
      promptInputRef.current?.focus();
      return;
    }
    if (reviewActionPendingRef.current) return;
    reviewActionPendingRef.current = true;
    setPendingReviewActionId(`refresh:${turnId}`);
    try {
      await loadSession(sessionId);
    } catch {
      updateTurnActionState(turnId, {
        actionError: 'The review could not be refreshed. Try again shortly.',
      });
    } finally {
      reviewActionPendingRef.current = false;
      setPendingReviewActionId(null);
    }
  };

  const handleProposalAction = async (options: {
    turnId: string;
    promptLabel: string;
    proposalId?: string | null;
    targetNodeId?: number | null;
    targetNodeIds?: number[];
    action: 'approve' | 'edit' | 'reject';
    payload?: Record<string, unknown>;
    proposalKind?: string;
  }) => {
    if (!options.proposalId) {
      updateTurnActionState(options.turnId, {
        actionStatus: 'failed',
        actionError: 'This proposal is missing its stable proposal id. Ask the AI workspace to generate a fresh one.',
      });
      return;
    }

    updateTurnActionState(options.turnId, { actionStatus: 'executing', actionError: null });
    const targetNodeId = isIntegerId(options.targetNodeId) ? options.targetNodeId : undefined;
    const strategistResult = await submitPrompt(options.promptLabel, {
      focusNodeId: targetNodeId !== undefined ? String(targetNodeId) : null,
      displayPrompt: formatWorkspaceCopy(options.promptLabel),
      uiEvent: {
        kind: 'proposal_action',
        sourceTurnId: options.turnId,
        targetNodeId,
        targetNodeIds: options.targetNodeIds ?? [],
        proposalId: options.proposalId,
        proposalKind: options.proposalKind,
        action: options.action,
        payload: {
          proposalLabel: options.promptLabel,
          ...options.payload,
        },
      },
    });

    if (!strategistResult) {
      updateTurnActionState(options.turnId, {
        actionStatus: 'failed',
        actionError: 'The AI workspace could not complete that proposal action.',
      });
      return;
    }
    const resultError = strategistResult.turn.error ?? strategistResult.run?.errorText ?? null;
    if (resultError) {
      updateTurnActionState(options.turnId, {
        actionStatus: 'failed',
        actionError: resultError,
      });
      return;
    }

    const actionResult = strategistResult.turn.response?.actionResult ?? null;
    if (options.proposalKind === 'schedule_patch' && actionResult) {
      if (actionResult.status === 'applied') {
        sharedResultGenerationRef.current += 1;
        const projectedProposalId = useCalendarStore.getState()
          .schedulePatchProposal?.proposal?.proposalId;
        if (projectedProposalId === options.proposalId) {
          markSchedulePatchProposalApplied();
        }
        const currentRange = calendarRangeRef.current;
        const notices: string[] = [];
        const refreshError = await refreshStrategistWorkspace();
        if (refreshError) {
          notices.push(refreshError);
        }
        if (currentRange) {
          await fetchCalendarBlocks(currentRange.startAt, currentRange.endAt, { force: useWorkspaceStore.getState().legacy });
          const calendarError = useCalendarStore.getState().error;
          if (calendarError) {
            notices.push(`Schedule applied, but the calendar refresh failed: ${calendarError}`);
          }
        }
        if (Array.isArray(actionResult.syncWarnings)) {
          notices.push(
            ...actionResult.syncWarnings.filter(
              (warning): warning is string => typeof warning === 'string',
            ),
          );
        }
        updateTurnActionState(options.turnId, {
          actionStatus: 'confirmed',
          actionError: notices.length > 0 ? notices.join(' ') : null,
        });
        focusTurnActionStatus(options.turnId);
        return;
      }
      if (actionResult.status === 'rejected') {
        sharedResultGenerationRef.current += 1;
        const projectedProposalId = useCalendarStore.getState()
          .schedulePatchProposal?.proposal?.proposalId;
        if (projectedProposalId === options.proposalId) {
          rejectSharedScheduleProposal();
        }
        updateTurnActionState(options.turnId, {
          actionStatus: 'dismissed',
          actionError: null,
        });
        focusTurnActionStatus(options.turnId);
        return;
      }
      if (actionResult.status === 'revised') {
        updateTurnActionState(options.turnId, {
          actionStatus: 'revised',
          actionError: null,
        });
        focusTurnActionStatus(options.turnId);
        return;
      }
      if (actionResult.status === 'stale') {
        updateTurnActionState(options.turnId, {
          actionStatus: 'stale',
          actionError: null,
        });
        focusTurnActionStatus(options.turnId);
        return;
      }
      if (actionResult.status === 'edit_requested') {
        setPrompt('');
        promptInputRef.current?.focus();
        updateTurnActionState(options.turnId, {
          actionStatus: 'idle',
          actionError: 'Describe the change in the composer, or choose one of the exact conflict alternatives.',
        });
        promptInputRef.current?.focus();
        return;
      }
    }

    if (options.action === 'approve') {
      const actionError = await refreshStrategistWorkspace();
      updateTurnActionState(options.turnId, {
        actionStatus: 'confirmed',
        actionError,
      });
      focusTurnActionStatus(options.turnId);
      return;
    }

    if (options.action === 'reject') {
      updateTurnActionState(options.turnId, {
        actionStatus: 'dismissed',
        actionError: null,
      });
      focusTurnActionStatus(options.turnId);
      return;
    }

    updateTurnActionState(options.turnId, {
      actionStatus: 'idle',
      actionError: null,
    });
  };

  const handleStandardProposalAction = async (options: {
    turnId: string;
    standardProposal: StrategistStandardProposal;
    action: 'approve' | 'edit' | 'reject';
  }) => {
    const { standardProposal } = options;
    await handleProposalAction({
      turnId: options.turnId,
      promptLabel:
        options.action === 'approve'
          ? `Approve ${standardProposal.title}`
          : options.action === 'edit'
            ? `Edit ${standardProposal.title}`
            : `Reject ${standardProposal.title}`,
      proposalId: standardProposal.proposalId,
      targetNodeId: standardProposal.targetNodeId,
      targetNodeIds: getStandardProposalTargetNodeIds(standardProposal),
      action: options.action,
      proposalKind: standardProposal.proposalKind,
    });
  };
  panelProposalActionHandlerRef.current = handleStandardProposalAction;

  const handleScheduleAlternative = async (options: {
    turnId: string;
    standardProposal: StrategistStandardProposal;
    conflictIndex: number;
    alternativeIndex: number;
  }) => {
    const { standardProposal } = options;
    await handleProposalAction({
      turnId: options.turnId,
      promptLabel: `Use this time for ${standardProposal.title}`,
      proposalId: standardProposal.proposalId,
      targetNodeId: standardProposal.targetNodeId,
      targetNodeIds: getStandardProposalTargetNodeIds(standardProposal),
      action: 'edit',
      proposalKind: 'schedule_patch',
      payload: {
        conflictIndex: options.conflictIndex,
        alternativeIndex: options.alternativeIndex,
      },
    });
  };

  useEffect(() => {
    if (
      !launchRequest
      || launchRequest.source !== 'schedule_proposal_action'
      || handledLaunchRequestIdsRef.current.has(launchRequest.requestId)
    ) {
      return;
    }
    const sourceTurn = turns.find(
      (turn) => turn.response?.proposal?.proposalId === launchRequest.proposalId,
    );
    if (!sourceTurn?.response?.proposal || !launchRequest.action) {
      if (isLoadingHistory) {
        return;
      }
      handledLaunchRequestIdsRef.current.add(launchRequest.requestId);
      onLaunchHandled(launchRequest.requestId);
      setHistoryError('Open the proposal chat before acting on this schedule.');
      return;
    }
    handledLaunchRequestIdsRef.current.add(launchRequest.requestId);
    const actionHandler = panelProposalActionHandlerRef.current;
    if (!actionHandler) {
      onLaunchHandled(launchRequest.requestId);
      return;
    }
    void actionHandler({
      turnId: sourceTurn.id,
      standardProposal: sourceTurn.response.proposal,
      action: launchRequest.action,
    }).finally(() => onLaunchHandled(launchRequest.requestId));
  }, [isLoadingHistory, launchRequest, onLaunchHandled, turns]);

  const handleClarificationChoice = async (
    turnId: string,
    choice: StrategistClarificationChoice,
  ) => {
    if (isLoading) {
      return;
    }
    const hasTypedDecision = Boolean(
      choice.goalId && choice.goalRevision && choice.decisionId && choice.optionId,
    );
    const eventId = hasTypedDecision
      ? `decision:${turnId}:${choice.decisionId}:${choice.optionId}`
      : undefined;
    await submitPrompt(hasTypedDecision ? '' : choice.label, {
      focusNodeId: choice.targetNodeId ? String(choice.targetNodeId) : currentTask?.id ?? null,
      displayPrompt: formatWorkspaceCopy(choice.label),
      uiEvent: {
        kind: 'clarification_choice',
        sourceTurnId: turnId,
        targetNodeId: choice.targetNodeId ?? undefined,
        goalId: choice.goalId ?? undefined,
        goalRevision: choice.goalRevision ?? undefined,
        decisionId: choice.decisionId ?? undefined,
        optionId: choice.optionId ?? undefined,
        eventId,
        idempotencyKey: eventId,
        payload: {
          choiceId: choice.value,
          choiceLabel: choice.label,
          ...(hasTypedDecision ? {} : { followUpPrompt: choice.followUpPrompt }),
        },
      },
    });
  };

  const handleResumeChangeSet = async (turnId: string, changeSetId: string) => {
    if (isLoading) {
      return;
    }
    await submitPrompt('', {
      displayPrompt: 'Resume preparing this review',
      uiEvent: {
        kind: 'resume_change_set',
        sourceTurnId: turnId,
        changeSetId,
      },
    });
  };

  const runtimeMessages = useMemo<ThreadMessageLike[]>(() => turns.flatMap((turn) => [
    { id: turn.id + ':user', role: 'user' as const, content: [{ type: 'text' as const, text: turn.prompt }] },
    {
      id: turn.id, role: 'assistant' as const,
      content: [
        ...(turn.reasoning ? [{ type: 'reasoning' as const, text: turn.reasoning }] : []),
        { type: 'text' as const, text: turn.response?.summary ?? turn.streamedAnswer ?? turn.error ?? '' },
      ],
      status: !turn.response && !turn.error && isLoading
        ? { type: 'running' as const }
        : turn.error ? { type: 'incomplete' as const, reason: 'error' as const, error: turn.error }
        : { type: 'complete' as const, reason: 'stop' as const },
    },
  ]), [turns, isLoading]);
  const chatRuntime = useExternalStoreRuntime({
    messages: runtimeMessages,
    convertMessage: (message) => message,
    isRunning: isLoading,
    onNew: async (message) => {
      const text = message.content.filter((part) => part.type === 'text').map((part) => part.text).join('\n');
      await submitPrompt(text);
    },
    onCancel: async () => { requestControllerRef.current?.abort(); },
    onReload: async (parentId) => {
      const turn = turns.find((item) => item.id + ':user' === parentId);
      if (turn) await submitPrompt(turn.prompt);
    },
  });
  const setPrompt = (text: string) => chatRuntime.thread.composer.setText(text);
  useEffect(() => {
    if (!launchRequest || !['custom_schedule', 'companion'].includes(launchRequest.source)) return;
    const frame = window.requestAnimationFrame(() => {
      if (launchRequest.source === 'companion' && launchRequest.prompt) chatRuntime.thread.composer.setText(launchRequest.prompt);
      promptInputRef.current?.focus();
      onLaunchHandled(launchRequest.requestId);
    });
    return () => window.cancelAnimationFrame(frame);
  }, [launchRequest, onLaunchHandled, chatRuntime.thread.composer]);
  const draftPrompt = (text: string) => {
    setPrompt(text);
    promptInputRef.current?.focus();
  };
  const visibleSessions = sessions.filter(item => item.title.toLocaleLowerCase().includes(historyQuery.trim().toLocaleLowerCase()));

  return (
    <AssistantRuntimeProvider runtime={chatRuntime}>
    <ThreadPrimitive.Root asChild>
    <article
      ref={surfaceRef}
      className={`planning-surface planning-ai-surface strategist-surface${isExpanded ? ' is-expanded' : ' is-docked'}${embedded ? ' is-embedded' : ''}`}
    >
      <div className="planning-surface-header strategist-surface-header">
        <div className="strategist-title-block">
          {!embedded && <h3 className="planning-surface-title strategist-workspace-title">
            AI workspace
          </h3>}
          <p className="strategist-session-title" title={activeSession?.title ?? 'New conversation'}>
            {activeSession?.title ?? 'New conversation'}
          </p>
        </div>

        <div className="strategist-header-actions">
          <button
            type="button"
            className="strategist-header-btn"
            onClick={startNewChat}
            disabled={isLoading}
            aria-label="New chat"
            title="New chat"
          >
            <NewChatIcon />
          </button>
          <button
            type="button"
            className="strategist-header-btn"
            onClick={() => setIsHistoryOpen((value) => !value)}
            aria-label="Chat history"
            title="Chat history"
            aria-expanded={isHistoryOpen}
            aria-controls="strategist-chat-history"
          >
            <HistoryIcon />
          </button>
          {!embedded && (
            <>
              <button
                type="button"
                className="strategist-header-btn"
                onClick={onToggleExpanded}
                aria-label={isExpanded ? 'Minimize AI workspace' : 'Expand AI workspace'}
                title={isExpanded ? 'Minimize' : 'Expand'}
              >
                <ExpandIcon expanded={isExpanded} />
              </button>
              <button
                type="button"
                className="strategist-header-btn close"
                onClick={() => {
                  sharedResultGenerationRef.current += 1;
                  onClose();
                }}
                aria-label="Close AI workspace"
                title="Close"
              >
                <CloseIcon />
              </button>
            </>
          )}
        </div>
      </div>

        {isHistoryOpen && (
          <section className="strategist-history-panel" id="strategist-chat-history" aria-label="Saved chats">
            <div className="strategist-history-header">
              <h4>Saved chats</h4>
              <button type="button" className="strategist-header-btn" aria-label="Close chat history" onClick={() => setIsHistoryOpen(false)}><X size={16} /></button>
            </div>
            <label className="chat-history-search"><Search size={16} aria-hidden="true" /><input type="search" aria-label="Search chats" placeholder="Search conversations" value={historyQuery} onChange={event => setHistoryQuery(event.target.value)} /></label>
            <div className="strategist-history-list">
              {visibleSessions.length === 0 ? (
                <div className="strategist-feedback strategist-feedback-pending">
                  {historyQuery ? 'No conversations match your search.' : 'Your conversations will appear here.'}
                </div>
              ) : (
                visibleSessions.map((sessionItem) => (
                  <button
                    key={sessionItem.id}
                    type="button"
                    className={`strategist-history-item${activeSession?.id === sessionItem.id ? ' is-active' : ''}`}
                    onClick={() => void loadSession(sessionItem.id)}
                    disabled={isLoadingHistory || isLoading}
                  >
                    <strong>{sessionItem.title}</strong>
                    <span>{formatSessionTime(sessionItem.updatedAt)}</span>
                  </button>
                ))
              )}
            </div>
          </section>
        )}

      <ThreadPrimitive.Viewport className="planning-ai-scroll strategist-scroll">
        {historyError && (
          <div className="strategist-feedback strategist-feedback-error" role="alert">
            {formatWorkspaceCopy(historyError)}
          </div>
        )}

        {isLoadingHistory && turns.length === 0 ? (
          <div className="strategist-feedback strategist-feedback-pending">
            Loading AI workspace history...
          </div>
        ) : turns.length === 0 ? (
          <div className="strategist-empty-state">
            <div className="strategist-empty-brand">
              <img src={drasilLogoUrl} alt="" className="strategist-empty-logo" />
              <h3 className="strategist-empty-title">A little clarity for your day.</h3>
            </div>
            <p className="strategist-empty-copy">Think it through, make a plan, or find time for what matters.</p>
            <div className="chat-starters" aria-label="Start a conversation">
              {[
                ['Plan my day', 'Help me make a realistic plan for today.'],
                ['Find my next step', 'Review my tasks and help me decide what to focus on next.'],
                ['Make time to focus', 'Help me find time for focused work this week.'],
              ].map(([label, prompt]) => <button type="button" key={label} onClick={() => draftPrompt(prompt)}>{label}<ArrowUp size={16} aria-hidden="true" /></button>)}
            </div>
          </div>
        ) : (
          <section className="strategist-response-stack" aria-label="Conversation">
            <ThreadPrimitive.Messages>{({ message }) => {
              if (message.role === 'user') return (
                <MessagePrimitive.Root className="chat-user-message">
                  <div className="strategist-turn strategist-turn-user"><MessagePrimitive.Parts /></div>
                  <ActionBarPrimitive.Root className="strategist-message-actions chat-user-actions">
                    <ActionBarPrimitive.Copy className="strategist-header-btn chat-copy" aria-label="Copy message" title="Copy message"><Copy size={15} className="chat-copy-default" /><Check size={15} className="chat-copy-done" /><span className="chat-copy-done" role="status">Copied</span></ActionBarPrimitive.Copy>
                    <button type="button" className="strategist-header-btn" aria-label="Reuse message" title="Reuse message in composer" disabled={isLoading} onClick={() => draftPrompt(message.content.filter(part => part.type === 'text').map(part => part.text).join('\n'))}><Pencil size={15} /></button>
                  </ActionBarPrimitive.Root>
                </MessagePrimitive.Root>
              );
              const turn = turns.find((item) => item.id === message.id);
              if (!turn) return null;
              const taskReferences = taskReferencesByTurnId.get(turn.id) ?? [];
              const taskReferenceById = new Map(
                taskReferences.map((reference) => [reference.node.id, reference]),
              );
              const reviewDocument = turn.response?.reviewDocument ?? null;
              const standardProposal = turn.response?.proposal ?? null;
              const clarification = turn.response?.clarification ?? null;
              const clarificationConflictGroups = clarification
                ? compactClarificationConflicts(clarification.conflicts ?? [])
                : [];
              const handoffOutcome = turn.response?.handoffOutcome ?? null;
              const clarificationFollowUps = clarification
                && clarification.entityChoices.length === 0
                && clarification.fieldChoices.length === 0
                ? getDistinctClarificationFollowUps(clarification)
                : [];
              const schedulePreviewIsDirty = Boolean(
                standardProposal?.proposalKind === 'schedule_patch'
                && activeScheduleProposal?.proposalId === standardProposal.proposalId
                && activeScheduleProposal?.schedulePatchStatus === 'dirty',
              );
              const canApplyProposal = canApproveStandardProposal(standardProposal)
                && !schedulePreviewIsDirty;
              const scheduleConflicts = getScheduleConflicts(standardProposal);
              const scheduleLifecycle = getScheduleProposalLifecycle(standardProposal);
              const isPreferenceProposal = standardProposal?.proposalKind === 'preference_memory';
              const preferenceRule = isPreferenceProposal && standardProposal
                ? getPreferenceRule(standardProposal)
                : null;
              const assistantSummary = isPreferenceProposal
                ? turn.actionStatus === 'confirmed'
                  ? "I'll use this preference for future schedules."
                  : turn.actionStatus === 'dismissed'
                    ? "I won't save this preference."
                    : 'I can remember this for future schedules.'
                : turn.response?.summary ?? '';
              const clarificationRepeatsSummary = Boolean(
                clarification
                && clarification.message.trim().toLocaleLowerCase()
                  === assistantSummary.trim().toLocaleLowerCase(),
              );
              const proposalBlockedReason =
                typeof standardProposal?.payload?.approval_blocked_reason === 'string'
                  ? standardProposal.payload.approval_blocked_reason
                  : null;
              return (
                <MessagePrimitive.Root key={turn.id} className="strategist-chat-group">
                  <ChatProgress reasoning={turn.reasoning} running={!turn.response && !turn.error && isLoading} activity={activityLabel} />
                  {!turn.response && turn.streamedAnswer && (
                    <div className="strategist-turn strategist-turn-assistant">
                      <ChatMarkdown text={turn.streamedAnswer} taskReferences={taskReferences} />
                    </div>
                  )}

                  {turn.error ? (
                    <div className="strategist-feedback strategist-feedback-error" role="alert">
                      <strong>{turn.error === 'Response stopped.' ? 'Response stopped' : 'Could not complete this message'}</strong>
                      <p>{formatWorkspaceCopy(turn.error)}</p>
                      <ActionBarPrimitive.Root className="chat-error-actions">
                        {/connect|sign.in|login|provider|API key/i.test(turn.error) && <button type="button" className="strategist-header-btn" onClick={() => useUiStore.getState().openSettings('connections')}>Open connections</button>}
                        <ActionBarPrimitive.Reload className="strategist-header-btn" aria-label="Retry message" title="Retry message">
                          <RotateCcw size={14} /> Retry
                        </ActionBarPrimitive.Reload>
                      </ActionBarPrimitive.Root>
                    </div>
                  ) : turn.response ? (
                    <>
                      {!reviewDocument && turn.response.trace && turn.response.trace.steps.length > 0 && (
                        <StrategistTraceTimeline trace={turn.response.trace} turnId={turn.id} />
                      )}
                      <div className="strategist-turn strategist-turn-assistant">
                        {!reviewDocument
                          && !clarificationRepeatsSummary
                          && renderDecisionSummary(assistantSummary, taskReferences)}

                      {handoffOutcome?.status === 'repairable_internally'
                        && handoffOutcome.changeSetId && (
                        <div className="planning-ai-chip-row strategist-chip-row">
                          <button
                            type="button"
                            className="planning-ai-chip-action"
                            onClick={() => void handleResumeChangeSet(
                              turn.id,
                              handoffOutcome.changeSetId as string,
                            )}
                            disabled={isLoading}
                          >
                            Resume review
                          </button>
                        </div>
                      )}

                      {handoffOutcome?.retryable === true
                        && handoffOutcome.requiresUserInput !== true
                        && !handoffOutcome.changeSetId && (
                        <div className="planning-ai-chip-row strategist-chip-row">
                          <button
                            type="button"
                            className="planning-ai-chip-action"
                            onClick={() => void submitPrompt(turn.prompt)}
                            disabled={isLoading}
                          >
                            Try again
                          </button>
                        </div>
                      )}

                      {clarification && (
                        <div className="strategist-action-card strategist-clarification-card">
                          <div className="strategist-action-copy">
                            <p className="strategist-action-title">Clarify before continuing</p>
                            <p className="strategist-action-body">
                              <RichAiText
                                text={formatWorkspaceCopy(clarification.message)}
                                taskReferences={taskReferences}
                              />
                            </p>
                          </div>

                          {clarificationConflictGroups.length > 0 && (
                            <div className="strategist-clarification-group">
                              <p className="strategist-clarification-label">Conflicts to resolve</p>
                              <ul className="strategist-action-list strategist-conflict-detail-list">
                                {clarificationConflictGroups.map((conflict, conflictIndex) => {
                                  const recommendation = conflict.recommendedAction === 'move_existing'
                                    ? `Suggested: keep ${conflict.incomingTaskTitle} at the requested time and move ${conflict.blockerTaskTitle}.`
                                    : conflict.recommendedAction === 'preserve_existing'
                                      ? `Suggested: keep ${conflict.blockerTaskTitle} and move ${conflict.incomingTaskTitle}.`
                                      : 'Decision needed: choose which commitment should keep this time.';
                                  const blockerTaskReference = taskReferences.find(
                                    ({ node }) => node.title.trim().toLocaleLowerCase()
                                      === conflict.blockerTaskTitle.trim().toLocaleLowerCase(),
                                  );
                                  return (
                                    <li
                                      key={`${conflict.date ?? conflict.requestedStartAt}-${conflict.blockerTaskTitle}-${conflictIndex}`}
                                    >
                                      <strong>
                                        <RichAiText
                                          text={conflict.blockerTaskTitle}
                                          taskReferences={taskReferences}
                                        />
                                        {conflict.blockerPriority && ` · ${conflict.blockerPriority} priority`}
                                      </strong>
                                      <span>{conflict.pattern} · {conflict.period}</span>
                                      {blockerTaskReference?.category && (
                                        <small>
                                          Existing task · {blockerTaskReference.category.name} workspace
                                        </small>
                                      )}
                                      <small>
                                        {formatClockOnly(conflict.blockerStartAt)}–{formatClockOnly(conflict.blockerEndAt)}
                                        {' overlaps '}
                                        <RichAiText
                                          text={conflict.incomingTaskTitle}
                                          taskReferences={taskReferences}
                                        />
                                        {' · '}
                                        {formatClockOnly(conflict.requestedStartAt)}–{formatClockOnly(conflict.requestedEndAt)}
                                      </small>
                                      {conflict.decisionReason && (
                                        <small>{formatWorkspaceCopy(conflict.decisionReason)}</small>
                                      )}
                                      {conflict.fixedReason && (
                                        <small>{formatWorkspaceCopy(conflict.fixedReason)}</small>
                                      )}
                                      <small className="strategist-conflict-recommendation">
                                        {recommendation}
                                      </small>
                                    </li>
                                  );
                                })}
                              </ul>
                            </div>
                          )}

                          {clarification.entityChoices.length > 0 && (
                            <div className="strategist-clarification-group">
                              <p className="strategist-clarification-label">Task choices</p>
                              <div className="strategist-clarification-grid">
                                {clarification.entityChoices.map((choice) => {
                                  const taskReference = choice.targetNodeId == null
                                    ? null
                                    : taskReferenceById.get(String(choice.targetNodeId)) ?? null;
                                  return (
                                    <button
                                      key={`${turn.id}-${choice.kind}-${choice.value}`}
                                      type="button"
                                      className="strategist-clarification-choice"
                                      onClick={() => void handleClarificationChoice(turn.id, choice)}
                                      disabled={isLoading}
                                    >
                                      <span className="strategist-clarification-choice-heading">
                                        {taskReference && (
                                          <span
                                            className="strategist-clarification-choice-emoji"
                                            aria-hidden="true"
                                          >
                                            {taskReference.node.emoji
                                              || suggestEmoji(
                                                taskReference.node.title,
                                                taskReference.node.type,
                                              )}
                                          </span>
                                        )}
                                        <strong>{formatWorkspaceCopy(choice.label)}</strong>
                                      </span>
                                      {choice.description && (
                                        <span className="strategist-clarification-choice-description">
                                          {formatWorkspaceCopy(choice.description)}
                                        </span>
                                      )}
                                    </button>
                                  );
                                })}
                              </div>
                            </div>
                          )}

                          {clarification.fieldChoices.length > 0 && (
                            <div className="strategist-clarification-group">
                              <p className="strategist-clarification-label">
                                {clarification.fieldChoices.some((choice) => choice.decisionId)
                                  ? 'Resolution choices'
                                  : 'Field choices'}
                              </p>
                              <div className="strategist-clarification-grid">
                                {clarification.fieldChoices.map((choice) => (
                                  <button
                                    key={`${turn.id}-${choice.kind}-${choice.value}`}
                                    type="button"
                                    className="strategist-clarification-choice is-field"
                                    onClick={() => void handleClarificationChoice(turn.id, choice)}
                                    disabled={isLoading}
                                  >
                                    <strong>{formatWorkspaceCopy(choice.label)}</strong>
                                    {choice.description && (
                                      <span className="strategist-clarification-choice-description">
                                        {formatWorkspaceCopy(choice.description)}
                                      </span>
                                    )}
                                  </button>
                                ))}
                              </div>
                            </div>
                          )}

                          {clarificationFollowUps.length > 0 && (
                            <div className="strategist-clarification-group">
                              <p className="strategist-clarification-label">Options</p>
                              <div className="planning-ai-chip-row strategist-chip-row">
                                {clarificationFollowUps.map((choicePrompt) => (
                                  <button
                                    key={`${turn.id}-${choicePrompt}`}
                                    type="button"
                                    className="planning-ai-chip-action"
                                    onClick={() =>
                                      void submitPrompt(choicePrompt, {
                                        displayPrompt: formatWorkspaceCopy(choicePrompt),
                                      })
                                    }
                                    disabled={isLoading}
                                  >
                                    {formatWorkspaceCopy(choicePrompt)}
                                  </button>
                                ))}
                              </div>
                            </div>
                          )}
                        </div>
                      )}

                      {reviewDocument && (
                        <ReviewDocumentCard
                          document={reviewDocument}
                          taskReferences={taskReferences}
                          busyActionId={pendingReviewActionId}
                          outcome={turn.reviewOutcome}
                          onAction={(action) => handleReviewAction(turn.id, action)}
                          onRecover={(recovery) => handleReviewRecovery(
                            turn.id,
                            reviewDocument.sessionId,
                            recovery,
                          )}
                          instanceId={turn.id}
                          statusRef={(node) => {
                            if (node) actionStatusRefs.current.set(turn.id, node);
                            else actionStatusRefs.current.delete(turn.id);
                          }}
                        />
                      )}
                      {reviewDocument && turn.actionError ? (
                        <div
                          className={`strategist-action-state ${turn.actionStatus === 'confirmed' ? 'warning' : 'error'}`}
                          role={turn.actionStatus === 'confirmed' ? 'status' : 'alert'}
                        >
                          {turn.actionError}
                        </div>
                      ) : null}

                      {!reviewDocument && standardProposal && (
                        <div className="strategist-action-card strategist-standard-proposal-card">
                          <div className="strategist-action-copy">
                            <p className="strategist-action-title">This proposal has expired</p>
                            <p className="strategist-action-body">
                              Ask the AI workspace to regenerate this request as a current
                              canonical review before making any changes.
                            </p>
                          </div>
                        </div>
                      )}

                      {RETIRED_COMPATIBILITY_ACTIONS_ENABLED && !reviewDocument && standardProposal && (
                        <div className={`strategist-action-card strategist-standard-proposal-card${isPreferenceProposal ? ' strategist-preference-proposal-card' : ''}`}>
                          <div className="strategist-action-copy">
                            <p className="strategist-action-title">
                              <RichAiText
                                text={isPreferenceProposal
                                  ? 'Remember this preference?'
                                  : formatWorkspaceCopy(standardProposal.title)}
                                taskReferences={taskReferences}
                              />
                            </p>
                            {isPreferenceProposal ? (
                              <p className="strategist-preference-rule">{preferenceRule}</p>
                            ) : (
                              <p className="strategist-action-body">
                                <RichAiText
                                  text={formatWorkspaceCopy(standardProposal.body)}
                                  taskReferences={taskReferences}
                                />
                              </p>
                            )}
                            {renderStandardProposalSupportContent(
                              standardProposal,
                              taskReferences,
                            )}
                            {scheduleConflicts.length > 0 && (
                              <div
                                className="strategist-clarification-group strategist-schedule-conflicts"
                                role="status"
                                aria-live="polite"
                              >
                                <p className="strategist-clarification-label">
                                  Conflicts and alternatives
                                </p>
                                <ul className="strategist-action-list">
                                  {scheduleConflicts.map((conflict, conflictIndex) => (
                                    <li key={`${conflict.taskId}-${conflict.type}-${conflictIndex}`}>
                                      <strong>{conflict.taskTitle ?? `Task ${conflict.taskId}`}</strong>
                                      <p>Couldn&apos;t fit this task in the requested time.</p>
                                      {(conflict.conflictingBlocks ?? []).map((block) => (
                                        <small key={block.blockId}>
                                          {`Conflicts with ${block.label ?? 'another calendar item'} · ${formatSessionTime(block.startAt)} – ${formatSessionTime(block.endAt)}`}
                                        </small>
                                      ))}
                                      {conflict.alternatives.map((alternative, alternativeIndex) => (
                                        <button
                                          key={`${alternative.startAt}-${alternative.endAt}-${alternativeIndex}`}
                                          type="button"
                                          className="planning-ghost-btn strategist-alternative-btn"
                                          onClick={() => void handleScheduleAlternative({
                                            turnId: turn.id,
                                            standardProposal,
                                            conflictIndex,
                                            alternativeIndex,
                                          })}
                                          disabled={
                                            isLoading
                                            || scheduleLifecycle !== 'pending'
                                            || !['idle', 'failed'].includes(turn.actionStatus)
                                          }
                                          aria-label={`Use ${formatSessionTime(alternative.startAt)} to ${formatSessionTime(alternative.endAt)} for ${conflict.taskTitle ?? `task ${conflict.taskId}`}`}
                                        >
                                          <span>
                                            Use {formatSessionTime(alternative.startAt)} - {formatSessionTime(alternative.endAt)}
                                          </span>
                                        </button>
                                      ))}
                                    </li>
                                  ))}
                                </ul>
                              </div>
                            )}
                            {renderBadgeSuggestions(standardProposal)}
                          </div>

                          {standardProposal.proposalKind === 'schedule_patch' ? (
                            turn.actionStatus === 'confirmed' || scheduleLifecycle === 'applied' ? (
                              <div
                                ref={(node) => {
                                  if (node) actionStatusRefs.current.set(turn.id, node);
                                  else actionStatusRefs.current.delete(turn.id);
                                }}
                                className="strategist-action-state success"
                                role="status"
                                tabIndex={-1}
                              >
                                Schedule applied.
                              </div>
                            ) : turn.actionStatus === 'revised' ? (
                              <div
                                ref={(node) => {
                                  if (node) actionStatusRefs.current.set(turn.id, node);
                                  else actionStatusRefs.current.delete(turn.id);
                                }}
                                className="strategist-action-state muted"
                                role="status"
                                tabIndex={-1}
                              >
                                A revised schedule replaced this proposal.
                              </div>
                            ) : turn.actionStatus === 'stale' ? (
                              <div
                                ref={(node) => {
                                  if (node) actionStatusRefs.current.set(turn.id, node);
                                  else actionStatusRefs.current.delete(turn.id);
                                }}
                                className="strategist-action-state warning"
                                role="status"
                                tabIndex={-1}
                              >
                                That time changed. Review the refreshed alternatives in the new proposal.
                              </div>
                            ) : turn.actionStatus === 'dismissed' || ['rejected', 'superseded'].includes(scheduleLifecycle) ? (
                              <div
                                ref={(node) => {
                                  if (node) actionStatusRefs.current.set(turn.id, node);
                                  else actionStatusRefs.current.delete(turn.id);
                                }}
                                className="strategist-action-state muted"
                                role="status"
                                tabIndex={-1}
                              >
                                {scheduleLifecycle === 'superseded'
                                  ? 'A revised schedule replaced this proposal.'
                                  : 'Schedule proposal rejected.'}
                              </div>
                            ) : (
                            <div className="strategist-action-row" aria-live="polite">
                              {canApplyProposal && (
                                <>
                                  <button
                                    type="button"
                                    className="strategist-confirm-btn"
                                    onClick={() => void handleStandardProposalAction({
                                      turnId: turn.id,
                                      standardProposal,
                                      action: 'approve',
                                    })}
                                    disabled={isLoading || turn.actionStatus === 'executing'}
                                  >
                                    {turn.actionStatus === 'executing' ? 'Applying...' : 'Approve schedule'}
                                  </button>
                                  <button
                                    type="button"
                                    className="planning-ghost-btn"
                                    onClick={() => void handleStandardProposalAction({
                                      turnId: turn.id,
                                      standardProposal,
                                      action: 'edit',
                                    })}
                                    disabled={isLoading || turn.actionStatus === 'executing'}
                                  >
                                    Edit request
                                  </button>
                                </>
                              )}
                              <button
                                type="button"
                                className="planning-ghost-btn"
                                onClick={() => void handleStandardProposalAction({
                                  turnId: turn.id,
                                  standardProposal,
                                  action: 'reject',
                                })}
                                disabled={isLoading || turn.actionStatus === 'executing'}
                              >
                                Reject
                              </button>
                              {!canApplyProposal && (
                                <span className="strategist-action-state muted">
                                  {schedulePreviewIsDirty
                                    ? 'This preview changed locally. Ask the AI workspace to regenerate and revalidate it before approval.'
                                    : 'This schedule proposal is not approvable. Review the validation details or reject it.'}
                                </span>
                              )}
                            </div>
                            )
                          ) : canApplyProposal ? (
                            turn.actionStatus === 'confirmed' ? (
                              <div
                                ref={(node) => {
                                  if (node) actionStatusRefs.current.set(turn.id, node);
                                  else actionStatusRefs.current.delete(turn.id);
                                }}
                                className="strategist-action-state success"
                                role="status"
                                tabIndex={-1}
                              >
                                {isPreferenceProposal ? 'Preference saved.' : 'Proposal applied.'}
                              </div>
                            ) : turn.actionStatus === 'revised' ? (
                              <div
                                ref={(node) => {
                                  if (node) actionStatusRefs.current.set(turn.id, node);
                                  else actionStatusRefs.current.delete(turn.id);
                                }}
                                className="strategist-action-state muted"
                                role="status"
                                tabIndex={-1}
                              >
                                A revised proposal replaced this one.
                              </div>
                            ) : turn.actionStatus === 'dismissed' ? (
                              <div
                                ref={(node) => {
                                  if (node) actionStatusRefs.current.set(turn.id, node);
                                  else actionStatusRefs.current.delete(turn.id);
                                }}
                                className="strategist-action-state muted"
                                role="status"
                                tabIndex={-1}
                              >
                                {isPreferenceProposal ? 'Preference not saved.' : 'Proposal rejected.'}
                              </div>
                            ) : (
                              <div className="strategist-action-row">
                                <button
                                  type="button"
                                  className="strategist-confirm-btn"
                                  onClick={() =>
                                    void handleStandardProposalAction({
                                      turnId: turn.id,
                                      standardProposal,
                                      action: 'approve',
                                    })
                                  }
                                  disabled={turn.actionStatus === 'executing'}
                                >
                                  {turn.actionStatus === 'executing'
                                    ? 'Saving...'
                                    : isPreferenceProposal ? 'Save preference' : 'Approve'}
                                </button>
                                <button
                                  type="button"
                                  className="planning-ghost-btn"
                                  onClick={() =>
                                    void handleStandardProposalAction({
                                      turnId: turn.id,
                                      standardProposal,
                                      action: 'edit',
                                    })
                                  }
                                  disabled={turn.actionStatus === 'executing'}
                                >
                                  {isPreferenceProposal ? 'Adjust' : 'Edit'}
                                </button>
                                <button
                                  type="button"
                                  className="planning-ghost-btn"
                                  onClick={() =>
                                    void handleStandardProposalAction({
                                      turnId: turn.id,
                                      standardProposal,
                                      action: 'reject',
                                    })
                                  }
                                  disabled={turn.actionStatus === 'executing'}
                                >
                                  {isPreferenceProposal ? 'Not now' : 'Reject'}
                                </button>
                              </div>
                            )
                          ) : (
                            <div className="strategist-action-state muted">
                              {formatWorkspaceCopy(proposalBlockedReason ??
                                (standardProposal.preferencePatchStatus === 'invalid'
                                  ? 'This memory proposal needs to be regenerated before it can be approved.'
                                  : standardProposal.taskPatchStatus === 'invalid'
                                  ? 'This proposal needs to be regenerated before it can be approved.'
                                  : 'This proposal is missing an approvable change set.'))}
                            </div>
                          )}

                          {turn.actionError && (
                            <div
                              className={`strategist-action-state ${turn.actionStatus === 'confirmed' ? 'warning' : 'error'}`}
                              role={turn.actionStatus === 'confirmed' ? 'status' : 'alert'}
                            >
                              {formatWorkspaceCopy(turn.actionError)}
                            </div>
                          )}
                        </div>
                      )}
                      </div>
                    </>
                  ) : (
                    <div className="strategist-pending-stack">
                      {runActivities.length > 1 && (
                        <details className="strategist-live-activity-panel">
                          <summary>{runActivities.length} steps</summary>
                          <ol>
                            {runActivities.map((activity) => {
                              const isThought = isReasoningActivity(activity);
                              return (
                                <li
                                  key={activity.id}
                                  className={`is-${activity.status}${isThought ? ' is-thought' : ''}`}
                                >
                                  {isThought ? (
                                    <details className="strategist-live-step">
                                      <summary>{activity.label}</summary>
                                      <p>{activity.label}</p>
                                    </details>
                                  ) : (
                                    <span className="strategist-live-step-label">{activity.label}</span>
                                  )}
                                </li>
                              );
                            })}
                          </ol>
                        </details>
                      )}
                    </div>
                  )}
                  {(turn.response || turn.streamedAnswer) && (
                    <ActionBarPrimitive.Root className="strategist-message-actions">
                      <ActionBarPrimitive.Copy className="strategist-header-btn chat-copy" aria-label="Copy response" title="Copy response"><Copy size={15} className="chat-copy-default" /><Check size={15} className="chat-copy-done" /><span className="chat-copy-done" role="status">Copied</span></ActionBarPrimitive.Copy>
                    </ActionBarPrimitive.Root>
                  )}
                </MessagePrimitive.Root>
              );
            }}</ThreadPrimitive.Messages>
          </section>
        )}
        {showPromptSuggestions && (
          <div className="strategist-followups strategist-followups-docked">
            <div className="planning-ai-chip-row strategist-chip-row" aria-label="Prompt suggestions">
              {promptSuggestions.map((suggestedPrompt) => (
                <button
                  key={suggestedPrompt}
                  type="button"
                  className="planning-ai-chip-action"
                  onClick={() =>
                    void submitPrompt(suggestedPrompt, {
                      displayPrompt: formatWorkspaceCopy(suggestedPrompt),
                    })
                  }
                  disabled={isLoading}
                >
                {formatWorkspaceCopy(suggestedPrompt)}
              </button>
              ))}
            </div>
          </div>
        )}
        <ThreadPrimitive.ScrollToBottom className="strategist-scroll-bottom" aria-label="Scroll to latest message"><ArrowDown size={16} /></ThreadPrimitive.ScrollToBottom>
      </ThreadPrimitive.Viewport>

      <div className="strategist-composer">
        {attachedTasks.length > 0 && (
          <div className="strategist-working-set" aria-label="Attached selected tasks">
            <span className="strategist-working-set-label">Selected tasks</span>
            <div className="strategist-working-set-chips">
              {attachedTasks.map(({ node, category }) => (
                <span
                  className="strategist-working-set-chip"
                  key={node.id}
                  aria-label={`${node.title}${category ? `, ${category.name}` : ''}`}
                >
                  <span aria-hidden="true">{node.emoji || suggestEmoji(node.title, node.type)}</span>
                  <span>{node.title}</span>
                  {category && <small>{category.name}</small>}
                </span>
              ))}
            </div>
          </div>
        )}
        <ComposerPrimitive.Root className="strategist-input-form">
          <div className="strategist-input-shell">
            <ComposerPrimitive.Input
              ref={promptInputRef}
              placeholder="Ask Drasil to plan, review, or schedule…"
              aria-label="AI workspace prompt"
              minRows={1}
              maxRows={8}
              unstable_insertNewlineOnTouchEnter
            />
            {isLoading ? (
              <ComposerPrimitive.Cancel className="strategist-submit-btn" aria-label="Stop response" title="Stop response">
                <Square size={16} />
              </ComposerPrimitive.Cancel>
            ) : (
              <ComposerPrimitive.Send className="strategist-submit-btn" aria-label="Send message" title="Send message">
                <ArrowUp size={18} />
              </ComposerPrimitive.Send>
            )}
          </div>
        </ComposerPrimitive.Root>
      </div>
    </article>
    </ThreadPrimitive.Root>
    </AssistantRuntimeProvider>
  );
}
