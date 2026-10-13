import { apiRequest, readJson } from './http';
import { apiFetch } from './connection';
import { readEventStream } from './eventStream';

const BASE = '/api/v1/ai/strategist';

export interface StrategistContextCard {
  id: string;
  label: string;
  value: string;
  detail?: string | null;
}

export interface StrategistBadgeSuggestion {
  targetNodeId?: number | null;
  targetNodeTitle?: string | null;
  emoji?: string | null;
  color?: string | null;
  label?: string | null;
  rationale?: string | null;
}

export interface StrategistStandardProposal {
  proposalId?: string | null;
  changeSetId?: string | null;
  changeSetStatus?: string | null;
  proposalKind: 'action' | 'metadata' | 'branch' | string;
  operation: string;
  legacyResponseKey: 'actionProposal' | 'metadataProposal' | 'branchProposal' | string;
  title: string;
  body: string;
  summary?: string | null;
  rationale?: string | null;
  targetNodeId?: number | null;
  targetNodeIds: number[];
  targetNodeTitle?: string | null;
  preview: string[];
  reviewSections?: {
    title: string;
    items: { text: string; taskIds: number[] }[];
  }[];
  validationWarnings: string[];
  taskPatch?: Record<string, unknown> | null;
  taskPatchValidation?: Record<string, unknown> | null;
  taskPatchSimulation?: Record<string, unknown> | null;
  taskPatchStatus?: string | null;
  preferencePatch?: Record<string, unknown> | null;
  preferencePatchValidation?: Record<string, unknown> | null;
  preferencePatchSimulation?: Record<string, unknown> | null;
  preferencePatchStatus?: string | null;
  badgeSuggestions?: StrategistBadgeSuggestion[] | null;
  schedulePatch?: Record<string, unknown> | null;
  schedulePatchValidation?: Record<string, unknown> | null;
  schedulePatchSimulation?: Record<string, unknown> | null;
  schedulePatchStatus?: string | null;
  capabilityId?: string | null;
  payload?: Record<string, unknown>;
}

interface StrategistClarificationChoiceBase {
  kind: 'entity' | 'field';
  label: string;
  value: string;
  description?: string | null;
  targetNodeId?: number | null;
}

export type StrategistClarificationChoice = StrategistClarificationChoiceBase & (
  | {
      followUpPrompt: string;
      goalId?: null;
      goalRevision?: null;
      decisionId?: null;
      optionId?: null;
    }
  | {
      followUpPrompt?: string;
      goalId: string;
      goalRevision: number;
      decisionId: string;
      optionId: string;
    }
);

export interface StrategistClarificationConflict {
  date?: string | null;
  incomingTaskTitle: string;
  incomingPriority?: string | null;
  requestedStartAt: string;
  requestedEndAt: string;
  blockerTaskTitle: string;
  blockerPriority?: string | null;
  blockerStartAt: string;
  blockerEndAt: string;
  fixedReason?: string | null;
  decisionReason?: string | null;
  recommendedAction?: string | null;
}

export interface StrategistClarificationPayload {
  message: string;
  entityChoices: StrategistClarificationChoice[];
  fieldChoices: StrategistClarificationChoice[];
  scopedFollowUpPrompts: string[];
  conflicts?: StrategistClarificationConflict[];
}

export interface StrategistTraceStep {
  kind: 'frame' | 'context' | 'plan' | 'tool' | 'decision' | 'timing' | string;
  title: string;
  detail: string;
  durationMs?: number | null;
  toolName?: string | null;
}

export interface StrategistTrace {
  summary: string;
  steps: StrategistTraceStep[];
}

export interface StrategistTaskReference {
  taskId: number;
  title: string;
  emoji?: string | null;
  nodeType: string;
  categoryId: number;
  categoryName?: string | null;
  categoryAccentColor?: string | null;
}

export interface StrategistReviewEntityRef {
  contractVersion: string;
  kind: string;
  entityId: string;
  revision?: number | null;
  title?: string | null;
  targetKind?: string | null;
}

export interface StrategistReviewEntity {
  contractVersion: string;
  revision: number;
  ref: StrategistReviewEntityRef;
  label: string;
  context?: string | null;
}

export interface StrategistReviewChange {
  contractVersion: string;
  revision: number;
  changeId: string;
  supportingOperationIds?: string[];
  summary: string;
  entities: StrategistReviewEntity[];
  before?: string | null;
  after?: string | null;
}

export interface StrategistReviewChangeGroup {
  contractVersion: string;
  revision: number;
  groupId: string;
  summary: string;
  changes: StrategistReviewChange[];
}

export interface StrategistTaskStructureSchedule {
  contractVersion: string;
  revision: number;
  rangeLabel: string;
  durationMinutes: number;
}

export interface StrategistTaskStructureItem {
  contractVersion: string;
  revision: number;
  task: StrategistReviewEntity;
  state: 'new' | 'existing';
  estimateMinutes?: number | null;
  deadlineAt?: string | null;
  scheduledBlocks: StrategistTaskStructureSchedule[];
  children: StrategistTaskStructureItem[];
}

export interface StrategistTaskStructureWorkspace {
  contractVersion: string;
  revision: number;
  workspace: StrategistReviewEntity;
  roots: StrategistTaskStructureItem[];
}

interface StrategistReviewBlockBase {
  contractVersion: string;
  revision: number;
  kind: string;
  blockId: string;
  title: string;
}

export type StrategistReviewBlock = StrategistReviewBlockBase & (
  | { kind: 'entity_references'; entities: StrategistReviewEntity[]; additionalEntities?: string | null }
  | { kind: 'task_structure'; workspaces: StrategistTaskStructureWorkspace[] }
  | { kind: 'grouped_changes'; groups: StrategistReviewChangeGroup[] }
  | { kind: 'semantic_diff'; changes: StrategistReviewChange[] }
  | { kind: 'schedule_summary'; cadence: string; rangeLabel: string; timeZone?: string | null; details: string[] }
  | { kind: 'recurrence_summary'; cadence: string; rangeLabel: string; scopeLabel: string; timeZone?: string | null; exceptions: string[]; additionalExceptions?: string | null }
  | { kind: 'conflict_resolution'; conflicts: StrategistConflictReviewItem[] }
  | { kind: 'assumptions'; statements: string[] }
  | { kind: 'warning'; severity: 'info' | 'warning' | 'critical'; message: string }
  | { kind: 'semantic_fallback'; summary: string; effects: StrategistReviewChange[] }
  | { kind: 'execution_status'; status: StrategistLocalExecutionStatus; summary: string; verificationStatus: StrategistVerificationStatus; verificationSummary: string; recovery?: 'reverify' | 'compensating_review' | 'manual_inspection' | null; executionId: string; commandId: string; idempotencyKey: string; replayed: boolean; bindings: StrategistExecutionBinding[]; facts: StrategistExecutionFact[]; syncItems: StrategistExternalSyncReviewStatus[] }
);

export interface StrategistConflictReviewItem {
  contractVersion: string;
  revision: number;
  conflictId: string;
  summary: string;
  resolution: string;
  affectedEntities: StrategistReviewEntity[];
  additionalAffectedEntities?: string | null;
  scopeLabel?: string | null;
  intervals: string[];
  additionalIntervals?: string | null;
  alternatives: string[];
  additionalAlternatives?: string | null;
}

export interface StrategistExternalSyncReviewStatus {
  contractVersion: string;
  revision: number;
  syncId: string;
  providerLabel: string;
  status: 'not_configured' | 'pending' | 'delivering' | 'succeeded' | 'failed';
  summary: string;
  attempts: number;
  retryAt?: string | null;
  errorCode?: string | null;
  recovery?: 'retry' | 'reconnect' | 'inspect' | null;
  remoteId?: string | null;
  remoteVersion?: string | null;
}

export interface StrategistExecutionBinding {
  contractVersion: string;
  revision: number;
  operationId: string;
  temporaryId?: string | null;
  entityRef: StrategistReviewEntityRef;
}

export interface StrategistExecutionFact {
  contractVersion: string;
  revision: number;
  operationId: string;
  disposition: 'present' | 'absent' | 'inactive';
  entityRef: StrategistReviewEntityRef;
  fields: Array<{ contractVersion: string; revision: number; field: string; valueJson: string }>;
  fingerprint: string;
}

export interface StrategistReviewActionBinding {
  contractVersion: string;
  revision: number;
  sessionId: string;
  goalId: string;
  goalRevision: number;
  changeSetId: string;
  changeSetRevision: number;
  simulationId: string;
  simulationInputFingerprint: string;
  simulationOutputFingerprint: string;
  correlationId: string;
  causationId: string;
}

interface StrategistReviewActionBase {
  contractVersion: string;
  revision: number;
  action: string;
  eventId: string;
  idempotencyKey: string;
  reviewId: string;
  reviewRevision: number;
  binding: StrategistReviewActionBinding;
}

export type StrategistReviewAction = StrategistReviewActionBase & (
  | { action: 'approve'; commandId: string }
  | { action: 'edit'; correction?: string | null }
  | { action: 'reject' }
  | { action: 'decision'; decisionId: string; optionId: string }
  | { action: 'retry'; executionId: string; originalCommandId: string; originalIdempotencyKey: string }
);

export interface StrategistReviewDecisionOption {
  optionId: string;
  label: string;
}

export interface StrategistReviewDecision {
  decisionId: string;
  question: string;
  reason: string;
  options: StrategistReviewDecisionOption[];
}

export interface StrategistReviewDocument {
  contractVersion: string;
  revision: number;
  reviewId: string;
  sessionId: string;
  goalId: string;
  goalRevision: number;
  changeSetId: string;
  changeSetRevision: number;
  simulationId: string;
  simulationInputFingerprint: string;
  simulationOutputFingerprint: string;
  correlationId: string;
  causationId: string;
  summary: string;
  blocks: StrategistReviewBlock[];
  decisions: StrategistReviewDecision[];
  controls: StrategistReviewAction[];
}

export type StrategistLocalExecutionStatus = 'committed_pending_verification' | 'applied' | 'verification_failed';
export type StrategistVerificationStatus = 'pending' | 'verified' | 'verification_failed';

export interface StrategistExecutionOutcome {
  status: 'ok' | 'error';
  result?: {
    executionId: string;
    commandId: string;
    idempotencyKey: string;
    goalRevision: number;
    changeSetRevision: number;
    status: StrategistLocalExecutionStatus;
    verification: {
      status: StrategistVerificationStatus;
      recovery?: 'reverify' | 'compensating_review' | 'manual_inspection' | null;
      recoverySummary?: string | null;
    };
    syncItems: Array<{
      outboxId: string;
      provider: string;
      status: 'not_configured' | 'pending' | 'delivering' | 'succeeded' | 'failed';
      attempts: number;
      nextAttemptAt?: string | null;
      errorSummary?: string | null;
      recovery?: 'retry' | 'reconnect' | 'inspect' | null;
      remoteVersion?: string | null;
    }>;
  } | null;
  error?: { code: string; userSummary: string; recovery: string } | null;
}

export interface StrategistReviewActionOutcome {
  status: 'ok' | 'error';
  result?: {
    status: 'execution_complete' | 'edit_requested' | 'rejected' | 'decision_recorded' | 'retry_replayed';
    reviewId: string;
    reviewRevision: number;
    sessionId: string;
    goalId: string;
    changeSetId: string;
    summary: string;
    goalRevision: number;
    changeSetRevision: number;
    correction?: string | null;
    execution?: StrategistExecutionOutcome | null;
  } | null;
  error?: {
    code: 'stale_review' | 'authorization_denied' | 'invalid_action' | 'system_failure';
    summary: string;
    recovery: 'refresh' | 'revise' | 'retry' | 'none';
  } | null;
}

export interface StrategistUiEvent {
  kind: 'clarification_choice' | 'proposal_action' | 'focus_change' | 'resume_change_set';
  sourceTurnId?: string;
  targetNodeId?: number;
  targetNodeIds?: number[];
  proposalId?: string;
  proposalKind?: string;
  changeSetId?: string;
  goalId?: string;
  goalRevision?: number;
  decisionId?: string;
  optionId?: string;
  eventId?: string;
  idempotencyKey?: string;
  action?: 'approve' | 'edit' | 'reject';
  payload?: Record<string, unknown>;
}

export interface StrategistResponse {
  providerLabel: string;
  mode: 'review' | 'metadata' | 'branch' | 'action' | 'schedule_patch' | 'preference_memory' | 'clarification' | string;
  summary: string;
  reasoningSummary?: string | null;
  contextCards: StrategistContextCard[];
  suggestedPrompts: string[];
  taskReferences?: StrategistTaskReference[];
  reviewDocument?: StrategistReviewDocument | null;
  proposal?: StrategistStandardProposal | null;
  clarification?: StrategistClarificationPayload | null;
  trace?: StrategistTrace | null;
  actionResult?: {
    proposalId?: string;
    sourceProposalId?: string;
    sourceTurnId?: string;
    status: 'applied' | 'rejected' | 'revised' | 'edit_requested' | string;
    idempotent?: boolean;
    createdBlockIds?: number[];
    updatedBlockIds?: number[];
    removedBlockIds?: number[];
    skippedTaskIds?: number[];
    operationCount?: number;
    [key: string]: unknown;
  } | null;
  handoffOutcome?: {
    status: 'ready_for_approval' | 'needs_user_input' | 'repairable_internally' | 'schedule_conflict' | 'unsupported' | 'system_failure' | string;
    message: string;
    changeSetId?: string | null;
    field?: string | null;
    reasonCode?: string | null;
    requiresUserInput?: boolean;
    retryable?: boolean;
    choices?: Array<{
      kind: string;
      label: string;
      value: string;
      follow_up_prompt?: string | null;
    }>;
  } | null;
}

export interface StrategistSessionSummary {
  id: string;
  title: string;
  providerLabel: string;
  modelName?: string | null;
  createdAt: string;
  updatedAt: string;
}

export interface StrategistTurnRecord {
  id: string;
  prompt: string;
  response?: StrategistResponse | null;
  error?: string | null;
  createdAt: string;
}

export interface StrategistRunSummary {
  id: string;
  sessionId: string;
  turnId: string;
  status: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled' | string;
  controlPlane?: string | null;
  prompt: string;
  errorText?: string | null;
  createdAt: string;
  startedAt?: string | null;
  completedAt?: string | null;
  updatedAt: string;
  activities?: StrategistRunActivity[];
}

export interface StrategistRunActivity {
  id: string;
  kind: string;
  label: string;
  status: 'completed' | 'active' | 'failed' | string;
  createdAt: string;
}

export interface StrategistSessionDetail {
  session: StrategistSessionSummary;
  turns: StrategistTurnRecord[];
}

export interface StrategistRunResponse {
  session: StrategistSessionSummary;
  turn: StrategistTurnRecord;
  run?: StrategistRunSummary | null;
}

export async function syncStrategistSessionContext(data: {
  sessionId: string;
  nodeIds: number[];
  source?: string;
  mentionText?: string | null;
}): Promise<void> {
  await apiRequest<void>(`${BASE}/sessions/${data.sessionId}/context`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      nodeIds: data.nodeIds,
      source: data.source ?? 'ui_apply',
      mentionText: data.mentionText ?? null,
    }),
  });
}

export async function fetchStrategistSessions(): Promise<StrategistSessionSummary[]> {
  return apiRequest<StrategistSessionSummary[]>(`${BASE}/sessions`);
}

export async function fetchStrategistSessionDetail(
  sessionId: string,
): Promise<StrategistSessionDetail> {
  return apiRequest<StrategistSessionDetail>(`${BASE}/sessions/${sessionId}`);
}

export interface StrategistProgress {
  kind: 'reasoning' | 'answer' | 'activity';
  text: string;
}

export async function requestStrategistAdvice(data: {
  prompt: string;
  sessionId?: string | null;
  activeCategoryId?: number | null;
  focusNodeId?: string | null;
  stagedNodeIds: string[];
  uiEvent?: StrategistUiEvent | null;
  controlPlane?: string | null;
  todayOverride?: string | null;
  calendarViewStart?: string | null;
  calendarViewEnd?: string | null;
  localDateTime?: string | null;
  timeZone?: string | null;
}, options?: {
  signal?: AbortSignal;
  onUpdate?: (result: StrategistRunResponse) => void;
  onProgress?: (progress: StrategistProgress) => void;
}): Promise<StrategistRunResponse> {
  const response = await apiFetch(BASE, {
    method: 'POST',
    signal: options?.signal,
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: JSON.stringify({
      prompt: data.prompt,
      sessionId: data.sessionId ?? null,
      activeCategoryId: data.activeCategoryId ?? null,
      focusNodeId: data.focusNodeId ? parseInt(data.focusNodeId, 10) : null,
      stagedNodeIds: data.stagedNodeIds
        .map((id) => parseInt(id, 10))
        .filter((id) => !Number.isNaN(id)),
      uiEvent: data.uiEvent ?? null,
      controlPlane: data.controlPlane ?? null,
      todayOverride: data.todayOverride ?? null,
      calendarViewStart: data.calendarViewStart ?? null,
      calendarViewEnd: data.calendarViewEnd ?? null,
      localDateTime: data.localDateTime ?? null,
      timeZone: data.timeZone ?? null,
    }),
  });
  if (!response.ok || !response.headers.get('content-type')?.includes('text/event-stream')) {
    return readJson<StrategistRunResponse>(response);
  }
  let final: StrategistRunResponse | undefined;
  await readEventStream(response, (name, payload) => {
    if (name === 'update' || name === 'done') {
      const result = payload as StrategistRunResponse;
      options?.onUpdate?.(result);
      if (name === 'done') final = result;
    } else if (name === 'progress') {
      const progress = payload as StrategistProgress;
      if (['reasoning', 'answer', 'activity'].includes(progress.kind) && typeof progress.text === 'string') {
        options?.onProgress?.(progress);
      }
    } else if (name === 'error') {
      throw new Error('The AI response was interrupted. Open the saved chat to check its status.');
    }
  });
  if (!final) throw new Error('The AI stream disconnected. Open the saved chat to check its status.');
  return final;
}

export async function fetchStrategistRun(runId: string): Promise<StrategistRunResponse> {
  return apiRequest<StrategistRunResponse>(`${BASE}/runs/${runId}`);
}

export async function submitStrategistReviewAction(
  sessionId: string,
  action: StrategistReviewAction,
): Promise<StrategistReviewActionOutcome> {
  const res = await apiFetch(`${BASE}/sessions/${sessionId}/review-actions`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(action),
  });
  if (res.ok) {
    return res.json() as Promise<StrategistReviewActionOutcome>;
  }

  try {
    const body = await res.json() as Partial<StrategistReviewActionOutcome>;
    const error = body.error;
    if (
      body.status === 'error'
      && error
      && ['stale_review', 'authorization_denied', 'invalid_action', 'system_failure'].includes(error.code)
      && typeof error.summary === 'string'
      && ['refresh', 'revise', 'retry', 'none'].includes(error.recovery)
    ) {
      return body as StrategistReviewActionOutcome;
    }
  } catch {
    // Fall through to the closed browser-owned error below.
  }

  return {
    status: 'error',
    error: {
      code: res.status === 401 || res.status === 403
        ? 'authorization_denied'
        : res.status === 409
          ? 'stale_review'
          : 'system_failure',
      summary: res.status === 401 || res.status === 403
        ? 'This review action is not available for this session.'
        : res.status === 409
          ? 'This review is out of date. Refresh it before continuing.'
          : 'Drasil could not complete this review action safely.',
      recovery: res.status === 409 ? 'refresh' : 'retry',
    },
  };
}

export function createStrategistRunEventSource(runId: string): EventSource {
  return new EventSource(`${BASE}/runs/${runId}/events`);
}
