import type {
  StrategistReviewAction,
  StrategistReviewActionOutcome,
  StrategistReviewBlock,
  StrategistReviewChange,
  StrategistReviewDocument,
  StrategistReviewEntity,
  StrategistTaskStructureItem,
} from '../../api/strategist';
import { useState } from 'react';
import type { ReactNode } from 'react';
import { CalendarClock, FolderTree } from 'lucide-react';
import { RichAiText, type AiTaskReference } from '../ai/RichAiText';
import './ReviewDocumentCard.css';

interface ReviewDocumentCardProps {
  document: StrategistReviewDocument;
  taskReferences: AiTaskReference[];
  busyActionId: string | null;
  outcome: StrategistReviewActionOutcome | null;
  onAction: (action: StrategistReviewAction) => void | Promise<void>;
  onRecover: (recovery: 'refresh' | 'revise') => void | Promise<void>;
  instanceId: string;
  statusRef?: (node: HTMLDivElement | null) => void;
}

const CANONICAL_TASK_REF_RE = /^task:(\d+)$/;

function ReviewCopy({ text, taskReferences }: { text: string; taskReferences: AiTaskReference[] }) {
  return <RichAiText text={text} taskReferences={taskReferences} />;
}

function ReviewDiffCopy({
  text,
  taskReferences,
}: {
  text: string;
  taskReferences: AiTaskReference[];
}) {
  const lines = text.replaceAll('\r\n', '\n').split('\n');
  const listItems = lines.slice(1);
  if (
    lines.length > 1
    && lines[0].trim()
    && listItems.every((line) => line.trimStart().startsWith('- '))
  ) {
    return (
      <div className="review-document-diff-copy">
        <ReviewCopy text={lines[0].trim()} taskReferences={taskReferences} />
        <ul>
          {listItems.map((line) => {
            const item = line.trimStart().slice(2).trim();
            return (
              <li key={item}>
                <ReviewCopy text={item} taskReferences={taskReferences} />
              </li>
            );
          })}
        </ul>
      </div>
    );
  }
  return <ReviewCopy text={text} taskReferences={taskReferences} />;
}

function uniqueCopy(values: string[]) {
  return [...new Set(values)];
}

function referencesForEntity(
  entity: StrategistReviewEntity,
  taskReferences: AiTaskReference[],
) {
  if (entity.ref.kind !== 'task') return taskReferences;
  const taskId = CANONICAL_TASK_REF_RE.exec(entity.ref.entityId)?.[1];
  if (!taskId || taskReferences.some(({ node }) => node.id === taskId)) {
    return taskReferences;
  }
  return [
    ...taskReferences,
    {
      node: {
        id: taskId,
        title: entity.label,
        type: 'task' as const,
      },
      category: null,
    },
  ];
}

function EntityList({
  entities,
  taskReferences,
}: {
  entities: StrategistReviewEntity[];
  taskReferences: AiTaskReference[];
}) {
  return (
    <ul className="review-document-entities">
      {entities.map((entity) => (
        <li key={`${entity.ref.kind}:${entity.ref.entityId}:${entity.revision}`}>
          <ReviewCopy
            text={entity.label}
            taskReferences={referencesForEntity(entity, taskReferences)}
          />
          {entity.context ? (
            <small><ReviewCopy text={entity.context} taskReferences={taskReferences} /></small>
          ) : null}
        </li>
      ))}
    </ul>
  );
}

function formatDuration(minutes: number) {
  const hours = Math.floor(minutes / 60);
  const remainder = minutes % 60;
  if (hours === 0) return `${minutes}m`;
  if (remainder === 0) return `${hours}h`;
  return `${hours}h ${remainder}m`;
}

function formatTaskDeadline(value: string) {
  const dateOnly = value.slice(0, 10);
  const [year, month, day] = dateOnly.split('-').map(Number);
  if (!year || !month || !day) return value;
  return new Intl.DateTimeFormat(undefined, {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
    timeZone: 'UTC',
  }).format(new Date(Date.UTC(year, month - 1, day)));
}

function TaskStructureNode({
  item,
  taskReferences,
}: {
  item: StrategistTaskStructureItem;
  taskReferences: AiTaskReference[];
}) {
  return (
    <li>
      <div className="review-document-tree-node">
        <span className="review-document-tree-title">
          <ReviewCopy text={item.task.label} taskReferences={taskReferences} />
        </span>
        <span className="review-document-tree-meta">
          {item.state === 'new' ? <small>New</small> : <small>Existing parent</small>}
          {item.estimateMinutes ? (
            <small>{formatDuration(item.estimateMinutes)} estimate</small>
          ) : null}
          {item.deadlineAt ? (
            <small>Due {formatTaskDeadline(item.deadlineAt)}</small>
          ) : null}
          {item.state === 'new'
            && item.children.length === 0
            && item.scheduledBlocks.length === 0 ? (
              <small>Not scheduled</small>
            ) : null}
        </span>
      </div>
      {item.scheduledBlocks.map((schedule) => (
        <div
          className="review-document-tree-schedule"
          key={`${schedule.rangeLabel}:${schedule.durationMinutes}`}
        >
          <CalendarClock size={13} aria-hidden="true" />
          <span>{formatDuration(schedule.durationMinutes)} scheduled</span>
          <small>{schedule.rangeLabel}</small>
        </div>
      ))}
      {item.children.length > 0 ? (
        <ul>
          {item.children.map((child) => (
            <TaskStructureNode
              key={child.task.ref.entityId}
              item={child}
              taskReferences={taskReferences}
            />
          ))}
        </ul>
      ) : null}
    </li>
  );
}

function ChangeList({
  changes,
  taskReferences,
}: {
  changes: StrategistReviewChange[];
  taskReferences: AiTaskReference[];
}) {
  return (
    <ul className="review-document-changes">
      {changes.map((change) => (
        <li key={change.changeId}>
          <strong><ReviewCopy text={change.summary} taskReferences={taskReferences} /></strong>
          {change.before ? (
            <div className="review-document-diff is-before">
              <span>Before</span>
              <ReviewDiffCopy text={change.before} taskReferences={taskReferences} />
            </div>
          ) : null}
          {change.after ? (
            <div className="review-document-diff is-after">
              <span>After</span>
              <ReviewDiffCopy text={change.after} taskReferences={taskReferences} />
            </div>
          ) : null}
        </li>
      ))}
    </ul>
  );
}

function ReviewBlock({
  block,
  taskReferences,
  headingId,
}: {
  block: StrategistReviewBlock;
  taskReferences: AiTaskReference[];
  headingId: string;
}) {
  let content: ReactNode;
  switch (block.kind) {
    case 'entity_references':
      content = (
        <>
          <EntityList entities={block.entities} taskReferences={taskReferences} />
          {block.additionalEntities ? <small>{block.additionalEntities}</small> : null}
        </>
      );
      break;
    case 'task_structure':
      content = (
        <div className="review-document-task-structure">
          {block.workspaces.map((workspace) => (
            <section key={workspace.workspace.ref.entityId}>
              <div className="review-document-workspace-label">
                <FolderTree size={14} aria-hidden="true" />
                <strong>
                  <ReviewCopy
                    text={workspace.workspace.label}
                    taskReferences={taskReferences}
                  />
                </strong>
                <small>workspace</small>
              </div>
              <ul className="review-document-task-tree">
                {workspace.roots.map((root) => (
                  <TaskStructureNode
                    key={root.task.ref.entityId}
                    item={root}
                    taskReferences={taskReferences}
                  />
                ))}
              </ul>
            </section>
          ))}
        </div>
      );
      break;
    case 'grouped_changes':
      content = (
        <div className="review-document-groups">
          {block.groups.map((group) => (
            <section key={group.groupId}>
              <h6>{group.summary}</h6>
              <ChangeList changes={group.changes} taskReferences={taskReferences} />
            </section>
          ))}
        </div>
      );
      break;
    case 'semantic_diff':
      content = <ChangeList changes={block.changes} taskReferences={taskReferences} />;
      break;
    case 'schedule_summary':
      content = (
        <div className="review-document-summary-grid">
          <strong>{block.cadence}</strong>
          <span>{block.rangeLabel}</span>
          {block.timeZone ? <small>Time zone: {block.timeZone}</small> : null}
          {block.details.length > 0 ? (
            <ul>{uniqueCopy(block.details).map((detail) => <li key={detail}>{detail}</li>)}</ul>
          ) : null}
        </div>
      );
      break;
    case 'recurrence_summary':
      content = (
        <div className="review-document-summary-grid">
          <strong>{block.cadence}</strong>
          <span>{block.rangeLabel}</span>
          <small>{block.scopeLabel}</small>
          {block.timeZone ? <small>Time zone: {block.timeZone}</small> : null}
          {block.exceptions.length > 0 ? (
            <ul>{uniqueCopy(block.exceptions).map((exception) => <li key={exception}>{exception}</li>)}</ul>
          ) : null}
          {block.additionalExceptions ? <small>{block.additionalExceptions}</small> : null}
        </div>
      );
      break;
    case 'conflict_resolution':
      content = (
        <ul className="review-document-conflicts">
          {block.conflicts.map((conflict) => (
            <li key={conflict.conflictId}>
              <strong><ReviewCopy text={conflict.summary} taskReferences={taskReferences} /></strong>
              <span><ReviewCopy text={conflict.resolution} taskReferences={taskReferences} /></span>
              <EntityList entities={conflict.affectedEntities} taskReferences={taskReferences} />
              {conflict.additionalAffectedEntities ? <small>{conflict.additionalAffectedEntities}</small> : null}
              {conflict.scopeLabel ? <small>{conflict.scopeLabel}</small> : null}
              {conflict.intervals.length > 0 ? (
                <ul>{uniqueCopy(conflict.intervals).map((item) => <li key={item}>{item}</li>)}</ul>
              ) : null}
              {conflict.additionalIntervals ? <small>{conflict.additionalIntervals}</small> : null}
              {conflict.alternatives.length > 0 ? (
                <details>
                  <summary>Other options</summary>
                  <ul>{uniqueCopy(conflict.alternatives).map((item) => <li key={item}>{item}</li>)}</ul>
                </details>
              ) : null}
              {conflict.additionalAlternatives ? <small>{conflict.additionalAlternatives}</small> : null}
            </li>
          ))}
        </ul>
      );
      break;
    case 'assumptions':
      content = <ul>{uniqueCopy(block.statements).map((statement) => <li key={statement}>{statement}</li>)}</ul>;
      break;
    case 'warning':
      content = <p className={`review-document-warning is-${block.severity}`}>{block.message}</p>;
      break;
    case 'semantic_fallback':
      content = (
        <>
          <p>{block.summary}</p>
          <ChangeList changes={block.effects} taskReferences={taskReferences} />
        </>
      );
      break;
    case 'execution_status':
      content = (
        <div className="review-document-summary-grid">
          <strong>{block.summary}</strong>
          <span>{block.verificationSummary}</span>
          {block.replayed ? <small>This is the durable result of the original request.</small> : null}
          {block.recovery ? (
            <small>
              {block.recovery === 'reverify'
                ? 'Verify the local changes again.'
                : block.recovery === 'compensating_review'
                  ? 'Review a corrective change before continuing.'
                  : 'Inspect the local changes before continuing.'}
            </small>
          ) : null}
          {block.syncItems.map((sync) => (
            <div key={sync.syncId} className={`review-document-sync is-${sync.status}`}>
              <span>{sync.providerLabel}</span>
              <small>{sync.summary}</small>
              {sync.attempts > 0 ? <small>Delivery attempts: {sync.attempts}</small> : null}
              {sync.retryAt ? <small>Next attempt: {sync.retryAt}</small> : null}
              {sync.recovery ? (
                <small>
                  {sync.recovery === 'retry'
                    ? 'Retry delivery with the original request.'
                    : sync.recovery === 'reconnect'
                      ? `Reconnect ${sync.providerLabel} before retrying.`
                      : `Inspect ${sync.providerLabel} delivery.`}
                </small>
              ) : null}
            </div>
          ))}
        </div>
      );
      break;
    default:
      content = <p>This review includes an additional change described in the summary above.</p>;
  }
  return (
    <section className={`review-document-block is-${block.kind}`} aria-labelledby={headingId}>
      <h5 id={headingId}>{block.title}</h5>
      {content}
    </section>
  );
}

function OutcomeStatus({
  outcome,
  statusRef,
}: {
  outcome: StrategistReviewActionOutcome;
  statusRef?: (node: HTMLDivElement | null) => void;
}) {
  if (outcome.status === 'error' || !outcome.result) {
    return (
      <div ref={statusRef} className="review-document-status is-warning" role="alert" tabIndex={-1}>
        {outcome.error?.summary ?? 'Drasil could not complete this review action safely.'}
      </div>
    );
  }
  const execution = outcome.result.execution;
  const local = execution?.result;
  if (execution?.status === 'error' || (execution && !local)) {
    return (
      <div ref={statusRef} className="review-document-status is-warning" role="alert" tabIndex={-1}>
        <strong>{execution.error?.userSummary ?? 'The reviewed changes were not applied.'}</strong>
        <span>Please review the current state before trying again.</span>
      </div>
    );
  }
  return (
    <div ref={statusRef} className="review-document-status" role="status" aria-live="polite" tabIndex={-1}>
      <strong>{outcome.result.summary}</strong>
      {local ? (
        <>
          <span>
            {local.status === 'applied'
              ? 'Local changes verified.'
              : local.status === 'verification_failed'
                ? local.verification.recoverySummary ?? 'The local changes committed but need inspection.'
                : 'Local changes committed and verification is pending.'}
          </span>
          {local.syncItems.length > 0 ? (
            <ul>
              {local.syncItems.map((sync) => (
                <li key={sync.outboxId}>
                  <span>{sync.provider === 'google' ? 'Google Calendar' : 'External calendar'}</span>
                  <small>{sync.errorSummary ?? sync.status.replace('_', ' ')}</small>
                  {sync.attempts > 0 ? <small>Delivery attempts: {sync.attempts}</small> : null}
                  {sync.nextAttemptAt ? <small>Next attempt: {sync.nextAttemptAt}</small> : null}
                  {sync.recovery ? (
                    <small>
                      {sync.recovery === 'retry'
                        ? 'Retry delivery with the original request.'
                        : sync.recovery === 'reconnect'
                          ? 'Reconnect the calendar before retrying.'
                          : 'Inspect the external calendar delivery.'}
                    </small>
                  ) : null}
                </li>
              ))}
            </ul>
          ) : null}
        </>
      ) : null}
    </div>
  );
}

export function ReviewDocumentCard({
  document,
  taskReferences,
  busyActionId,
  outcome,
  onAction,
  onRecover,
  instanceId,
  statusRef,
}: ReviewDocumentCardProps) {
  const [editAction, setEditAction] = useState<StrategistReviewAction | null>(null);
  const [editCorrection, setEditCorrection] = useState('');
  const decisionLabels = new Map(
    document.decisions.flatMap((decision) =>
      decision.options.map((option) => [
        `${decision.decisionId}:${option.optionId}`,
        { label: option.label, question: decision.question },
      ]),
    ),
  );
  const nestedExecutionFailed = outcome?.status === 'ok'
    && outcome.result?.execution?.status === 'error';
  const controlsDisabled = busyActionId !== null || (
    outcome?.status === 'ok' && !nestedExecutionFailed
  );
  const controlsVisible = !outcome || (
    outcome.status === 'error' && outcome.error?.recovery === 'retry'
  ) || (nestedExecutionFailed && outcome.result?.execution?.error?.recovery === 'retry');
  const idPrefix = `${instanceId}-${document.reviewId}-${document.revision}`;
  const titleId = `${idPrefix}-title`;
  return (
    <article className="review-document-card" aria-labelledby={titleId}>
      <header>
        <h4 id={titleId}>
          <ReviewCopy text={document.summary} taskReferences={taskReferences} />
        </h4>
      </header>

      <div className="review-document-blocks">
        {document.blocks.map((block) => {
          const content = <ReviewBlock block={block} taskReferences={taskReferences}
            headingId={`${idPrefix}-${block.blockId}-title`} />;
          return block.kind === 'entity_references'
            ? <details className="review-supporting-details" key={block.blockId}><summary>Included tasks</summary>{content}</details>
            : <div key={block.blockId}>{content}</div>;
        })}
      </div>

      {document.decisions.map((decision) => (
        <section key={decision.decisionId} className="review-document-decision">
          <h5>{decision.question}</h5>
          <p>{decision.reason}</p>
        </section>
      ))}

      {outcome ? <OutcomeStatus outcome={outcome} statusRef={statusRef} /> : null}
      {outcome?.status === 'ok' && outcome.result?.status === 'decision_recorded' ? (
        <button
          type="button"
          className="review-document-secondary-action"
          disabled={busyActionId !== null}
          onClick={() => void onRecover('revise')}
        >
          Continue review in the prompt
        </button>
      ) : null}
      {outcome?.status === 'error' && outcome.error?.recovery === 'refresh' ? (
        <button
          type="button"
          className="review-document-secondary-action"
          disabled={busyActionId !== null}
          onClick={() => void onRecover('refresh')}
        >
          Refresh review
        </button>
      ) : null}
      {outcome?.status === 'error' && outcome.error?.recovery === 'revise' ? (
        <button
          type="button"
          className="review-document-secondary-action"
          disabled={busyActionId !== null}
          onClick={() => void onRecover('revise')}
        >
          Revise request
        </button>
      ) : null}
      {controlsVisible ? (
        <div className="review-document-actions" aria-label="Review actions">
          {document.controls.map((control) => {
            const decisionCopy = control.action === 'decision'
              ? decisionLabels.get(`${control.decisionId}:${control.optionId}`)
              : null;
            const label = control.action === 'decision'
              ? decisionCopy?.label ?? 'Choose this option'
              : control.action === 'approve'
                ? 'Apply changes'
                : control.action === 'edit'
                  ? 'Revise'
                  : control.action === 'reject'
                    ? 'Reject'
                    : 'Retry';
            return (
              <button
                key={control.eventId}
                type="button"
                className={control.action === 'approve' ? 'review-document-primary-action' : 'review-document-secondary-action'}
                onClick={() => {
                  if (control.action === 'edit') {
                    setEditAction(control);
                    setEditCorrection(control.correction ?? '');
                    return;
                  }
                  void onAction(control);
                }}
                disabled={controlsDisabled}
                aria-describedby={titleId}
                aria-label={decisionCopy ? `${decisionCopy.question}: ${decisionCopy.label}` : undefined}
              >
                {busyActionId === control.eventId ? `${label}...` : label}
              </button>
            );
          })}
        </div>
      ) : null}
      {editAction?.action === 'edit' && controlsVisible ? (
        <form
          className="review-document-edit-form"
          onSubmit={(event) => {
            event.preventDefault();
            const correction = editCorrection.trim();
            if (!correction) return;
            void onAction({ ...editAction, correction });
          }}
        >
          <label htmlFor={`${idPrefix}-edit-correction`}>What should change?</label>
          <textarea
            id={`${idPrefix}-edit-correction`}
            value={editCorrection}
            maxLength={10_000}
            rows={3}
            autoFocus
            disabled={controlsDisabled}
            onChange={(event) => setEditCorrection(event.target.value)}
          />
          <div className="review-document-actions">
            <button
              type="button"
              className="review-document-secondary-action"
              disabled={controlsDisabled}
              onClick={() => setEditAction(null)}
            >
              Cancel
            </button>
            <button
              type="submit"
              className="review-document-primary-action"
              disabled={controlsDisabled || editCorrection.trim().length === 0}
            >
              Submit edit
            </button>
          </div>
        </form>
      ) : null}
    </article>
  );
}
