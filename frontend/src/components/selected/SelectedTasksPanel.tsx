import { useEffect, useRef, useState } from 'react';
import type { CSSProperties } from 'react';
import type { DragEvent as ReactDragEvent } from 'react';
import type { Category } from '../../types/category';
import type { NodeDragPayload, StrategyNode } from '../../types/node';
import type { PlannerProposeResult } from '../../types/planner';
import type { AutoScheduleDraftResult } from '../../types/scheduling';
import type { AppDestination } from '../../store/useUiStore';
import { useNodeStore } from '../../store/useNodeStore';
import { suggestEmoji } from '../../lib/emoji';
import './SelectedTasksPanel.css';

const RETIRED_COMPATIBILITY_ACTIONS_ENABLED = false;

interface SelectedTasksPanelProps {
  embedded?: boolean;
  activeDestination: AppDestination;
  stagedTasks: Array<{
    node: StrategyNode;
    category: Category | null;
  }>;
  stagedTaskCount: number;
  stagedEstimateMinutes: number;
  scheduledTaskIdSet: Set<string>;
  scheduleDraftResult: AutoScheduleDraftResult | null;
  isNodesLoading: boolean;
  onStageDroppedTask: (id: string) => void;
  onRemoveSelectedTask: (id: string) => void;
  onReorderSelectedTask: (id: string, targetId: string, dropIntent: 'before' | 'after') => void;
  onAutoScheduleSelected: () => void;
  isAutoScheduling: boolean;
  isPlannerProposing: boolean;
  isPlannerProposalActionPending: boolean;
  plannerProposal: PlannerProposeResult | null;
  plannerProposalError: string | null;
  onPlannerProposalAction: (
    proposalId: string,
    action: 'approve' | 'edit' | 'reject',
  ) => void;
}

type SelectedDropIntent = 'before' | 'after';

const utilityModeCopy: Record<Exclude<AppDestination, 'all' | 'category'>, string> = {
  'my-day':
    'My Day should summarize the work you are actually steering today without pretending those items are a separate category.',
  inbox:
    'Inbox should stage uncategorized work before it becomes part of a structured tree or calendar commitment.',
  settings:
    'Settings belongs to system operations, not to task hierarchy or scheduling state.',
};

function formatTime(minutes?: number | null) {
  if (!minutes) return '--';
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const remainder = minutes % 60;
  return remainder ? `${hours}h ${remainder}m` : `${hours}h`;
}

function formatDeadline(deadlineAt?: string | null) {
  if (!deadlineAt) return 'No deadline';

  const parsedDate = new Date(`${deadlineAt}T00:00:00`);
  if (Number.isNaN(parsedDate.getTime())) {
    return 'No deadline';
  }

  return new Intl.DateTimeFormat('en-US', {
    month: 'short',
    day: 'numeric',
  }).format(parsedDate);
}

function priorityTone(priority: StrategyNode['priority']) {
  if (priority === 'high') return 'red';
  if (priority === 'low') return 'green';
  return 'blue';
}

function displayNodeEmoji(node: StrategyNode) {
  return node.emoji || suggestEmoji(node.title, node.type);
}

function readDragPayload(
  event: ReactDragEvent<HTMLElement>,
  fallbackPayload: NodeDragPayload | null,
): NodeDragPayload | null {
  const customPayload = event.dataTransfer.getData('application/x-drasil-node');
  if (customPayload) {
    try {
      return JSON.parse(customPayload) as NodeDragPayload;
    } catch {
      return fallbackPayload;
    }
  }

  const textPayload = event.dataTransfer.getData('text/plain');
  if (textPayload) {
    try {
      return JSON.parse(textPayload) as NodeDragPayload;
    } catch {
      return fallbackPayload;
    }
  }

  return fallbackPayload;
}

interface StagedTaskTreeNodeProps {
  node: StrategyNode;
  expandedNodeIds: Record<string, boolean>;
  onToggleExpanded: (id: string) => void;
  onRemoveSelectedTask: (id: string) => void;
  isRoot?: boolean;
  depth?: number;
  categoryAccentColor?: string | null;
  scheduledTaskIdSet: Set<string>;
}

function StagedTaskTreeNode({
  node,
  expandedNodeIds,
  onToggleExpanded,
  onRemoveSelectedTask,
  isRoot = false,
  depth = 0,
  categoryAccentColor = null,
  scheduledTaskIdSet,
}: StagedTaskTreeNodeProps) {
  const hasChildren = node.children.length > 0;
  const isExpanded = expandedNodeIds[node.id] ?? true;
  const rowStyle = {
    paddingLeft: `${12 + depth * 20}px`,
  } as CSSProperties;

  return (
    <div className={`selected-stage-node${isRoot ? ' root' : ''}`}>
      <div
        className={`selected-stage-node-row${isRoot ? ' root' : ''}${
          hasChildren ? ' branch' : ''
        }`}
        style={rowStyle}
      >
        <div className="selected-stage-node-leading">
          {hasChildren ? (
            <button
              type="button"
              className={`selected-stage-toggle${isExpanded ? ' expanded' : ''}`}
              onClick={() => onToggleExpanded(node.id)}
              aria-label={isExpanded ? `Collapse ${node.title}` : `Expand ${node.title}`}
            >
              {'>'}
            </button>
          ) : (
            <span className="selected-stage-toggle-spacer" aria-hidden="true" />
          )}

          {isRoot ? (
            <span className="selected-row-icon" aria-hidden="true">
              {displayNodeEmoji(node)}
            </span>
          ) : (
            <span className="selected-row-bullet" aria-hidden="true" />
          )}

          {isRoot ? (
            <h4
              className="selected-stage-root-title"
              style={{ color: categoryAccentColor ?? '#c86c16' }}
            >
              {node.title}
            </h4>
          ) : (
            <span className="selected-stage-node-title">{node.title}</span>
          )}
        </div>

        <div className="selected-row-meta">
          {isRoot && node.isBacklog && <span className="selected-state-pill">Backlog</span>}
          {scheduledTaskIdSet.has(node.id) && (
            <span className="selected-state-pill scheduled">Scheduled</span>
          )}
          <span>{formatDeadline(node.deadlineAt)}</span>
          <span
            className={`selected-priority-dot tone-${priorityTone(node.priority)}`}
            role="img"
            aria-label={`${node.priority} priority`}
          />
          <span>{formatTime(node.timeEstimateMinutes)}</span>
          {isRoot && (
            <button
              type="button"
              className="selected-row-remove-btn icon"
              onClick={() => onRemoveSelectedTask(node.id)}
              aria-label={`Remove ${node.title} from selected tasks`}
              title="Remove"
            >
              x
            </button>
          )}
        </div>
      </div>

      {hasChildren && isExpanded && (
        <div className="selected-stage-node-children">
          {node.children.map((child) => (
            <StagedTaskTreeNode
              key={child.id}
              node={child}
              expandedNodeIds={expandedNodeIds}
              onToggleExpanded={onToggleExpanded}
              onRemoveSelectedTask={onRemoveSelectedTask}
              depth={depth + 1}
              scheduledTaskIdSet={scheduledTaskIdSet}
            />
          ))}
        </div>
      )}
    </div>
  );
}

export function SelectedTasksPanel({
  embedded = false,
  activeDestination,
  stagedTasks,
  stagedTaskCount,
  stagedEstimateMinutes,
  scheduledTaskIdSet,
  scheduleDraftResult,
  isNodesLoading,
  onStageDroppedTask,
  onRemoveSelectedTask,
  onReorderSelectedTask,
  onAutoScheduleSelected,
  isAutoScheduling,
  isPlannerProposing,
  isPlannerProposalActionPending,
  plannerProposal,
  plannerProposalError,
  onPlannerProposalAction,
}: SelectedTasksPanelProps) {
  const isWorkspaceView =
    activeDestination === 'category' || activeDestination === 'all';
  const hasStagedTasks = stagedTaskCount > 0;
  const activeDragPayload = useNodeStore((state) => state.activeDragPayload);
  const isTreeDragActive = activeDragPayload !== null;
  const [isDropHovering, setIsDropHovering] = useState(false);
  const [draggingSelectedTaskId, setDraggingSelectedTaskId] = useState<string | null>(null);
  const [selectedDropTarget, setSelectedDropTarget] = useState<{
    id: string;
    intent: SelectedDropIntent;
  } | null>(null);
  const [expandedNodeIds, setExpandedNodeIds] = useState<Record<string, boolean>>({});
  const dropPayloadRef = useRef<NodeDragPayload | null>(null);
  const selectedDragPayloadRef = useRef<NodeDragPayload | null>(null);
  const stagedRootIds = stagedTasks.map(({ node }) => node.id);
  const stagedRootIdSet = new Set(stagedRootIds);
  const rawProposal = plannerProposal?.proposal ?? null;
  const proposal = rawProposal
    && rawProposal.schedulePatchValidation.operationCount > 0
    ? rawProposal
    : null;
  const emptyProposal = rawProposal
    && rawProposal.schedulePatchValidation.operationCount <= 0;
  const proposalStatus = proposal?.schedulePatchStatus ?? null;
  const lifecycleValue = proposal?.payload?.schedulePatchLifecycle;
  const proposalLifecycle = lifecycleValue && typeof lifecycleValue === 'object'
    && !Array.isArray(lifecycleValue)
    && typeof (lifecycleValue as Record<string, unknown>).status === 'string'
    ? String((lifecycleValue as Record<string, unknown>).status)
    : 'pending';
  const canReviewProposal = proposalLifecycle === 'pending'
    && (proposalStatus === 'valid' || proposalStatus === 'warning');
  const proposalBlocks = proposal?.schedulePatchSimulation.previewBlocks ?? [];
  const proposalMinutes = proposalBlocks.reduce(
    (total, block) => total + plannerBlockDuration(block.startAt, block.endAt),
    0,
  );
  const stagedNodeById = new Map<string, StrategyNode>();
  const indexStagedNode = (node: StrategyNode) => {
    stagedNodeById.set(node.id, node);
    node.children.forEach(indexStagedNode);
  };
  stagedTasks.forEach(({ node }) => indexStagedNode(node));

  const handleToggleExpanded = (id: string) => {
    setExpandedNodeIds((current) => ({
      ...current,
      [id]: !(current[id] ?? true),
    }));
  };

  useEffect(() => {
    const clearDropHover = () => {
      dropPayloadRef.current = null;
      selectedDragPayloadRef.current = null;
      setIsDropHovering(false);
      setDraggingSelectedTaskId(null);
      setSelectedDropTarget(null);
    };

    window.addEventListener('dragend', clearDropHover);
    window.addEventListener('drop', clearDropHover);

    return () => {
      window.removeEventListener('dragend', clearDropHover);
      window.removeEventListener('drop', clearDropHover);
    };
  }, []);

  const handleListSurfaceDragEnter = (event: ReactDragEvent<HTMLElement>) => {
    const payload = readDragPayload(event, useNodeStore.getState().activeDragPayload);
    if (!payload) {
      return;
    }

    dropPayloadRef.current = payload;
    setIsDropHovering(true);
  };

  const handleListSurfaceDragOver = (event: ReactDragEvent<HTMLElement>) => {
    const payload = readDragPayload(event, useNodeStore.getState().activeDragPayload);
    if (!payload) {
      return;
    }

    event.preventDefault();
    event.stopPropagation();
    event.dataTransfer.dropEffect = 'copy';
    dropPayloadRef.current = payload;
    setIsDropHovering(true);
  };

  const handleListSurfaceDragLeave = (event: ReactDragEvent<HTMLElement>) => {
    if (event.currentTarget.contains(event.relatedTarget as Node | null)) {
      return;
    }

    setIsDropHovering(false);
  };

  const handleListSurfaceDrop = (event: ReactDragEvent<HTMLElement>) => {
    event.preventDefault();
    event.stopPropagation();
    const payload =
      readDragPayload(event, useNodeStore.getState().activeDragPayload) ?? dropPayloadRef.current;
    dropPayloadRef.current = null;
    setIsDropHovering(false);

    if (!payload) {
      return;
    }

    onStageDroppedTask(payload.id);
    useNodeStore.getState().setActiveDragPayload(null);
  };

  const readSelectedReorderPayload = (
    event: ReactDragEvent<HTMLElement>,
  ): NodeDragPayload | null =>
    selectedDragPayloadRef.current ??
    useNodeStore.getState().activeDragPayload ??
    readDragPayload(event, null);

  const updateSelectedDropTarget = (
    id: string,
    intent: SelectedDropIntent,
  ) => {
    setSelectedDropTarget((current) =>
      current?.id === id && current.intent === intent ? current : { id, intent },
    );
  };

  const handleSelectedDragStart =
    (node: StrategyNode) => (event: ReactDragEvent<HTMLElement>) => {
      if (
        event.target instanceof HTMLElement &&
        event.target.closest('button, input, textarea, select')
      ) {
        event.preventDefault();
        return;
      }

      event.stopPropagation();
      const payload: NodeDragPayload = {
        id: node.id,
        parentId: node.parentId ?? null,
        categoryId: node.categoryId,
        sortOrder: node.sortOrder ?? 0,
      };
      selectedDragPayloadRef.current = payload;
      setDraggingSelectedTaskId(node.id);
      const serializedPayload = JSON.stringify(payload);
      event.dataTransfer.setData('application/x-drasil-node', serializedPayload);
      event.dataTransfer.setData('text/plain', serializedPayload);
      event.dataTransfer.effectAllowed = 'copyMove';
    };

  const handleSelectedDragEnd = (event: ReactDragEvent<HTMLElement>) => {
    event.stopPropagation();
    selectedDragPayloadRef.current = null;
    setDraggingSelectedTaskId(null);
    setSelectedDropTarget(null);
  };

  const handleSelectedReorderDragOver =
    (targetId: string, intent: SelectedDropIntent) =>
    (event: ReactDragEvent<HTMLElement>) => {
      const payload = readSelectedReorderPayload(event);
      if (!payload || payload.id === targetId) {
        return;
      }

      event.preventDefault();
      event.stopPropagation();
      event.dataTransfer.dropEffect = stagedRootIdSet.has(payload.id) ? 'move' : 'copy';
      updateSelectedDropTarget(targetId, intent);
    };

  const handleSelectedReorderDrop =
    (targetId: string, intent: SelectedDropIntent) =>
    (event: ReactDragEvent<HTMLElement>) => {
      event.preventDefault();
      event.stopPropagation();
      const payload = readSelectedReorderPayload(event);
      selectedDragPayloadRef.current = null;
      setDraggingSelectedTaskId(null);
      setSelectedDropTarget(null);
      setIsDropHovering(false);

      if (!payload || payload.id === targetId) {
        return;
      }

      if (!stagedRootIdSet.has(payload.id)) {
        onStageDroppedTask(payload.id);
      }
      onReorderSelectedTask(payload.id, targetId, intent);
      useNodeStore.getState().setActiveDragPayload(null);
    };

  const readDropIntentFromCard = (
    event: ReactDragEvent<HTMLElement>,
    targetId: string,
  ): SelectedDropIntent => {
    const rect = event.currentTarget.getBoundingClientRect();
    const payload = readSelectedReorderPayload(event);
    const sourceIndex = payload ? stagedRootIds.indexOf(payload.id) : -1;
    const targetIndex = stagedRootIds.indexOf(targetId);
    const dropY = event.clientY - rect.top;
    const topQuarter = rect.height * 0.25;
    const bottomQuarter = rect.height * 0.75;

    if (sourceIndex >= 0 && targetIndex >= 0) {
      if (sourceIndex > targetIndex && dropY < bottomQuarter) {
        return 'before';
      }
      if (sourceIndex < targetIndex && dropY > topQuarter) {
        return 'after';
      }
    }

    return dropY < rect.height / 2 ? 'before' : 'after';
  };

  const handleSelectedCardDragOver =
    (targetId: string) => (event: ReactDragEvent<HTMLElement>) => {
      const intent = readDropIntentFromCard(event, targetId);
      handleSelectedReorderDragOver(targetId, intent)(event);
    };

  const handleSelectedCardDrop =
    (targetId: string) => (event: ReactDragEvent<HTMLElement>) => {
      const intent = readDropIntentFromCard(event, targetId);
      handleSelectedReorderDrop(targetId, intent)(event);
    };

  if (!isWorkspaceView) {
    return (
      <section
        className={`selected-panel${embedded ? ' is-embedded' : ''}`}
        aria-labelledby={embedded ? undefined : 'selected-panel-heading'}
        aria-label={embedded ? 'Selected tasks' : undefined}
      >
        {!embedded && <header className="selected-panel-header">
          <div>
            <h2 className="selected-panel-title" id="selected-panel-heading">
              Selected tasks
            </h2>
            <p className="selected-panel-subtitle">Planner staging and negotiation</p>
          </div>
        </header>}

        <div className="selected-panel-placeholder">
          <article className="selected-surface selected-empty-surface">
            <p className="selected-empty-copy">
              {
                utilityModeCopy[
                  activeDestination as Exclude<AppDestination, 'all' | 'category'>
                ]
              }
            </p>
          </article>
        </div>
      </section>
    );
  }

  return (
    <section
      className={`selected-panel${embedded ? ' is-embedded' : ''}`}
      aria-labelledby={embedded ? undefined : 'selected-panel-heading'}
      aria-label={embedded ? 'Selected tasks' : undefined}
    >
      {!embedded && <header className="selected-panel-header">
        <div>
          <h2 className="selected-panel-title" id="selected-panel-heading">
            Selected tasks
          </h2>
          <p className="selected-panel-subtitle">Planner staging and negotiation</p>
        </div>
      </header>}

      <div className="selected-panel-body">
        <article
          className={`selected-surface selected-list-surface${
            isTreeDragActive ? ' can-drop' : ''
          }${isDropHovering ? ' drop-hover' : ''}`}
          onDragEnter={handleListSurfaceDragEnter}
          onDragOver={handleListSurfaceDragOver}
          onDragLeave={handleListSurfaceDragLeave}
          onDrop={handleListSurfaceDrop}
        >
          <div className="selected-surface-header">
            <div className="selected-heading-row">
              <h3 className="selected-surface-title">Selected tasks</h3>
              <span className="selected-count-badge">{stagedTaskCount}</span>
            </div>
            {hasStagedTasks && (
              <div className="selected-proposal-metrics" aria-label="Selected task summary">
                <span>{formatTime(stagedEstimateMinutes)}</span>
              </div>
            )}
          </div>

          <div className={`selected-list-scroll${hasStagedTasks ? ' has-content' : ' is-empty'}`}>
            {stagedTasks.map(({ node, category }) => (
              <div
                key={node.id}
                className="selected-task-drag-wrapper"
                onDragOver={handleSelectedCardDragOver(node.id)}
                onDragEnter={handleSelectedCardDragOver(node.id)}
                onDrop={handleSelectedCardDrop(node.id)}
                style={
                  {
                    '--selected-stage-accent': category?.accentColor ?? '#5f8ef8',
                  } as CSSProperties
                }
              >
                <div
                  className={`selected-task-drop-lane before${
                    selectedDropTarget?.id === node.id &&
                    selectedDropTarget.intent === 'before'
                      ? ' active'
                      : ''
                  }`}
                  aria-hidden="true"
                />
                <article
                  className={`selected-task-group${
                    draggingSelectedTaskId === node.id ? ' dragging' : ''
                  }`}
                  draggable
                  onDragStart={handleSelectedDragStart(node)}
                  onDragEnd={handleSelectedDragEnd}
                  onDragOver={handleSelectedCardDragOver(node.id)}
                  onDragEnter={handleSelectedCardDragOver(node.id)}
                  onDrop={handleSelectedCardDrop(node.id)}
                >
                  <StagedTaskTreeNode
                    node={node}
                    expandedNodeIds={expandedNodeIds}
                    onToggleExpanded={handleToggleExpanded}
                    onRemoveSelectedTask={onRemoveSelectedTask}
                    isRoot
                    depth={0}
                    categoryAccentColor={category?.accentColor ?? null}
                    scheduledTaskIdSet={scheduledTaskIdSet}
                  />
                  <div
                    className={`selected-task-drop-lane after-row${
                      selectedDropTarget?.id === node.id &&
                      selectedDropTarget.intent === 'after'
                        ? ' active'
                        : ''
                    }`}
                    aria-hidden="true"
                  />
                </article>
              </div>
            ))}

            {!isNodesLoading && stagedTaskCount === 0 && (
              <div className="selected-empty-state">
                <p className="selected-empty-title">No selected tasks yet</p>
                <p className="selected-empty-copy">
                  Stage tasks from the active tree to prepare them for execution and planning.
                </p>
              </div>
            )}
          </div>
        </article>

        <div className="selected-action-row">
          <button
            type="button"
            className="selected-primary-btn"
            disabled={!hasStagedTasks || isAutoScheduling || isPlannerProposing}
            onClick={onAutoScheduleSelected}
          >
            {isAutoScheduling ? 'Drafting schedule' : 'Auto-schedule selected'}
            <span>{isAutoScheduling ? 'Solving open calendar slots' : 'Schedule now with constraints'}</span>
          </button>
        </div>

        {scheduleDraftResult && (
          <article className="selected-surface selected-solver-result" aria-live="polite">
            <div className="selected-proposal-metrics" aria-label="Solver summary">
              <span>{scheduleDraftResult.diagnostics.scheduledTaskCount} drafted</span>
              <span>{scheduleDraftResult.diagnostics.alreadyScheduledTaskIds.length} already scheduled</span>
              <span>
                {formatTime(scheduleDraftResult.draftBlocks.reduce(
                  (total, block) => total + plannerBlockDuration(block.startAt, block.endAt),
                  0,
                ))}
              </span>
              <span>{Math.round(scheduleDraftResult.diagnostics.runtimeMs)}ms</span>
            </div>
            <ul className="solver-list" aria-label="Drafted placements">
              {scheduleDraftResult.draftBlocks.map((block) => (
                <li key={block.id}>
                  <strong>{block.title}</strong>
                  <span>{formatPlannerTime(block.startAt)} - {formatPlannerTime(block.endAt)}</span>
                </li>
              ))}
            </ul>
            {scheduleDraftResult.diagnostics.alreadyScheduledTaskIds.length > 0 && (
              <ul className="solver-list" aria-label="Already scheduled tasks">
                {scheduleDraftResult.diagnostics.alreadyScheduledTaskIds.map((id) => (
                  <li key={id}>
                    Already on the calendar: {stagedNodeById.get(id)?.title ?? 'Selected task'}
                  </li>
                ))}
              </ul>
            )}
          </article>
        )}

        {(plannerProposalError || plannerProposal) && (
          <article className="selected-surface selected-schedulepatch-card" aria-live="polite">
            {plannerProposalError ? (
              <div className="selected-proposal-message is-error">
                <strong>Planner proposal unavailable</strong>
                <span>{plannerProposalError}</span>
              </div>
            ) : proposal ? (
              <>
                <div className="selected-proposal-heading">
                  <div>
                    <span className={`selected-proposal-status status-${proposalLifecycle === 'applied' ? 'applied' : proposalStatus}`}>
                      {proposalLifecycle === 'applied'
                        ? 'Applied'
                        : canReviewProposal ? 'Ready for review' : 'Needs attention'}
                    </span>
                    <h3>{proposal.title}</h3>
                    <p>{proposal.summary ?? proposal.body}</p>
                  </div>
                  <div className="selected-proposal-metrics" aria-label="Schedule duration">
                    <span>{formatTime(proposalMinutes)}</span>
                  </div>
                </div>

                {proposalBlocks.length > 0 && (
                  <ul className="selected-proposal-placements" aria-label="Proposed placements">
                    {proposalBlocks.map((block) => (
                      <li key={block.id}>
                        <strong>{block.title}</strong>
                        <span>
                          {formatPlannerTime(block.startAt)} - {formatPlannerTime(block.endAt)}
                        </span>
                      </li>
                    ))}
                  </ul>
                )}

                {(proposal.schedulePatchValidation.errors.length > 0 ||
                  proposal.schedulePatchValidation.warnings.length > 0 ||
                  (proposal.schedulePatchSimulation.warnings?.length ?? 0) > 0) && (
                  <ul className="selected-proposal-notices" aria-label="Schedule review notes">
                    <li>Some scheduling details need attention before this plan can be approved.</li>
                  </ul>
                )}

                {(proposal.schedulePatchSimulation.conflicts ?? proposal.payload?.conflicts ?? [])
                  .length > 0 && (
                  <div className="selected-proposal-conflicts">
                    <strong>Conflicts and alternatives</strong>
                    <ul>
                      {(proposal.schedulePatchSimulation.conflicts ?? proposal.payload?.conflicts ?? []).map(
                        (conflict) => (
                          <li key={`${conflict.taskId}-${conflict.type}`}>
                            <strong>{conflict.taskTitle ?? 'Selected task'}</strong>
                            <span>Couldn&apos;t fit this task in the requested time.</span>
                            {(conflict.conflictingBlocks ?? []).map((block) => (
                              <small key={block.blockId}>
                                Conflicts with {block.label ?? 'another calendar item'} · {formatPlannerTime(block.startAt)} - {formatPlannerTime(block.endAt)}
                              </small>
                            ))}
                            {conflict.alternatives.map((alternative) => (
                              <small key={`${alternative.startAt}-${alternative.endAt}`}>
                                Next opening: {formatPlannerTime(alternative.startAt)} - {formatPlannerTime(alternative.endAt)}
                              </small>
                            ))}
                          </li>
                        ),
                      )}
                    </ul>
                  </div>
                )}

                {canReviewProposal && (
                  <div className="selected-proposal-message" role="status">
                    <strong>This proposal has expired</strong>
                    <span>
                      Ask the AI workspace to regenerate it as a current canonical review.
                    </span>
                  </div>
                )}

                {RETIRED_COMPATIBILITY_ACTIONS_ENABLED && canReviewProposal && (
                  <div className="selected-proposal-actions">
                    <button
                      type="button"
                      disabled={isPlannerProposalActionPending}
                      onClick={() => onPlannerProposalAction(proposal.proposalId, 'approve')}
                    >
                      {isPlannerProposalActionPending ? 'Working...' : 'Approve'}
                    </button>
                    <button
                      type="button"
                      disabled={isPlannerProposalActionPending}
                      onClick={() => onPlannerProposalAction(proposal.proposalId, 'edit')}
                    >
                      Edit
                    </button>
                    <button
                      type="button"
                      disabled={isPlannerProposalActionPending}
                      onClick={() => onPlannerProposalAction(proposal.proposalId, 'reject')}
                    >
                      Reject
                    </button>
                  </div>
                )}

                {proposalLifecycle === 'applied' && (
                  <div className="selected-proposal-message" role="status">
                    <strong>Schedule applied</strong>
                    <span>Accepted calendar blocks are now the source of truth.</span>
                  </div>
                )}
              </>
            ) : emptyProposal ? (
              <div className="selected-proposal-message">
                <strong>No schedule proposed</strong>
                <span>Try a wider time range, split the task into sessions, or choose another time.</span>
              </div>
            ) : (
              <div className="selected-proposal-message">
                <strong>{plannerProposal?.providerLabel ?? 'Planner'}</strong>
                <span>{plannerProposal?.summary}</span>
              </div>
            )}
          </article>
        )}
      </div>
    </section>
  );
}

function formatPlannerTime(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat('en-US', {
    hour: 'numeric',
    minute: '2-digit',
  }).format(date);
}

function plannerBlockDuration(startAt: string, endAt: string) {
  const start = new Date(startAt).getTime();
  const end = new Date(endAt).getTime();
  if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) return 0;
  return Math.round((end - start) / 60_000);
}
