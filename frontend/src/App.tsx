import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type {
  KeyboardEvent as ReactKeyboardEvent,
  PointerEvent as ReactPointerEvent,
  ReactNode,
} from 'react';
import './styles/globals.css';
import './App.css';
import { CalendarPanel } from './components/calendar/CalendarPanel';
import type { CalendarPointerDropHandler } from './components/calendar/CalendarPanel';
import { AppSidebar } from './components/sidebar/AppSidebar';
import { PlanningWorkspacePanel } from './components/planning/PlanningWorkspacePanel';
import { StrategistAiCard } from './components/planning/StrategistAiCard';
import { SelectedTasksPanel } from './components/selected/SelectedTasksPanel';
import { MyDayAgenda } from './components/agenda/MyDayAgenda';
import { useCategoryStore } from './store/useCategoryStore';
import { useCalendarStore } from './store/useCalendarStore';
import { useNodeStore } from './store/useNodeStore';
import { useUiStore } from './store/useUiStore';
import type { NodeDragPayload, NodeStatus, StrategyNode } from './types/node';
import type { Category } from './types/category';
import { suggestEmoji } from './lib/emoji';
import { isArchivedNode, isActiveTreeNode, isBacklogTreeNode } from './lib/nodeState';

const FutureStatePreview = lazy(() =>
  import('./components/future/FutureStatePreview').then((module) => ({
    default: module.FutureStatePreview,
  })),
);

export interface AiWorkspaceLaunchRequest {
  requestId: string;
  source: 'custom_schedule' | 'schedule_proposal_action';
  stagedNodeIds: string[];
  proposalId?: string;
  action?: 'approve' | 'edit' | 'reject';
}

type PanelKey = 'planning' | 'ai' | 'calendar';
type AiWorkspaceTab = 'chat' | 'selected';
type ResizeBoundary = 'planning-ai' | 'ai-calendar';

interface WorkspacePanelWidths {
  planning: number;
  ai: number;
}

const PANEL_RAIL_WIDTH = '42px';
const PLANNING_EXPANDED_WIDTH = 'minmax(0, 1.24fr)';
const AI_BASE_WIDTH = 'minmax(360px, 0.98fr)';
const AI_EXPANDED_WIDTH = 'minmax(0, 1.12fr)';
const CALENDAR_BASE_WIDTH = 'minmax(360px, 1.08fr)';
const CALENDAR_EXPANDED_WIDTH = 'minmax(0, 1.28fr)';
const EQUAL_SHARE_WIDTH = 'minmax(0, 1fr)';
const DEFAULT_PANEL_WIDTHS: WorkspacePanelWidths = { planning: 350, ai: 380 };
const PANEL_WIDTH_STORAGE_KEY = 'drasil.workspace-panel-widths.v1';

function clampPanelWidth(value: number, minimum: number, maximum: number) {
  return Math.min(maximum, Math.max(minimum, Math.round(value)));
}

function loadPanelWidths(): WorkspacePanelWidths {
  try {
    const stored = window.localStorage.getItem(PANEL_WIDTH_STORAGE_KEY);
    if (!stored) return DEFAULT_PANEL_WIDTHS;
    const parsed = JSON.parse(stored) as Partial<WorkspacePanelWidths>;
    return {
      planning: clampPanelWidth(parsed.planning ?? DEFAULT_PANEL_WIDTHS.planning, 330, 620),
      ai: clampPanelWidth(parsed.ai ?? DEFAULT_PANEL_WIDTHS.ai, 360, 720),
    };
  } catch {
    return DEFAULT_PANEL_WIDTHS;
  }
}

function PanelToggleIcon({
  collapsed,
  dock,
}: {
  collapsed: boolean;
  dock: 'left' | 'right';
}) {
  const dividerX = dock === 'left' ? 7 : 13;

  return (
    <svg viewBox="0 0 20 20" className="workspace-panel-toggle-icon" aria-hidden="true">
      <rect x="3" y="4" width="14" height="12" rx="3" />
      <path d={`M${dividerX} 5.5v9`} />
      <path d="M8 10h4" />
      {collapsed && <path d="M10 8v4" />}
    </svg>
  );
}

function RailIcon({ panelKey }: { panelKey: PanelKey }) {
  if (panelKey === 'planning') {
    return (
      <svg viewBox="0 0 20 20" className="workspace-panel-collapsed-icon" aria-hidden="true">
        <path d="M5 4.5h10M5 10h6M5 15.5h10" />
        <circle cx="13.5" cy="10" r="1.5" />
      </svg>
    );
  }

  if (panelKey === 'ai') {
    return (
      <svg viewBox="0 0 20 20" className="workspace-panel-collapsed-icon" aria-hidden="true">
        <path d="M7.5 5h7M7.5 10h7M7.5 15h7" />
        <circle cx="4.5" cy="5" r="1" />
        <circle cx="4.5" cy="10" r="1" />
        <circle cx="4.5" cy="15" r="1" />
      </svg>
    );
  }

  return (
    <svg viewBox="0 0 20 20" className="workspace-panel-collapsed-icon" aria-hidden="true">
      <rect x="3.5" y="5" width="13" height="10.5" rx="2.5" />
      <path d="M3.5 8h13M7 3.5v3M13 3.5v3" />
    </svg>
  );
}

function getDesktopShellColumns(
  isSidebarCollapsed: boolean,
  collapsedPanels: Record<PanelKey, boolean>,
  panelWidths: WorkspacePanelWidths,
) {
  const withBoundaries = (planning: string, ai: string, calendar: string) =>
    `${planning} 10px ${ai} 10px ${calendar}`;
  const aiBase = AI_BASE_WIDTH;
  const calendarBase = isSidebarCollapsed
    ? 'minmax(360px, 1.18fr)'
    : CALENDAR_BASE_WIDTH;
  const planningExpanded = isSidebarCollapsed
    ? 'minmax(0, 1.3fr)'
    : PLANNING_EXPANDED_WIDTH;
  const calendarExpanded = isSidebarCollapsed
    ? 'minmax(0, 1.36fr)'
    : CALENDAR_EXPANDED_WIDTH;

  if (collapsedPanels.planning && collapsedPanels.ai && collapsedPanels.calendar) {
    return withBoundaries(PANEL_RAIL_WIDTH, PANEL_RAIL_WIDTH, PANEL_RAIL_WIDTH);
  }
  if (collapsedPanels.planning && collapsedPanels.ai) {
    return withBoundaries(PANEL_RAIL_WIDTH, PANEL_RAIL_WIDTH, 'minmax(0, 1fr)');
  }
  if (collapsedPanels.planning && collapsedPanels.calendar) {
    return withBoundaries(PANEL_RAIL_WIDTH, AI_EXPANDED_WIDTH, PANEL_RAIL_WIDTH);
  }
  if (collapsedPanels.ai && collapsedPanels.calendar) {
    return withBoundaries('minmax(0, 1fr)', PANEL_RAIL_WIDTH, PANEL_RAIL_WIDTH);
  }
  if (collapsedPanels.planning) {
    return withBoundaries(PANEL_RAIL_WIDTH, aiBase, calendarExpanded);
  }
  if (collapsedPanels.ai) {
    return withBoundaries(EQUAL_SHARE_WIDTH, PANEL_RAIL_WIDTH, EQUAL_SHARE_WIDTH);
  }
  if (collapsedPanels.calendar) {
    return withBoundaries(planningExpanded, aiBase, PANEL_RAIL_WIDTH);
  }

  return withBoundaries(`${panelWidths.planning}px`, `${panelWidths.ai}px`, calendarBase);
}

function flattenNodes(nodes: StrategyNode[]): StrategyNode[] {
  return nodes.flatMap((node) => [node, ...flattenNodes(node.children)]);
}

function buildCompletionCascadeUpdates(
  id: string,
  done: boolean,
  nodes: StrategyNode[],
): Record<string, NodeStatus> {
  const flatNodes = flattenNodes(nodes);
  const nodeById = new Map(flatNodes.map((node) => [node.id, node]));
  const targetNode = nodeById.get(id);
  const targetStatus: NodeStatus = done ? 'done' : 'todo';

  if (!targetNode) {
    return { [id]: targetStatus };
  }

  const updates = new Map<string, NodeStatus>();
  const setSubtreeStatus = (node: StrategyNode) => {
    updates.set(node.id, targetStatus);
    node.children.forEach(setSubtreeStatus);
  };

  setSubtreeStatus(targetNode);

  let parentId = targetNode.parentId ?? null;
  while (parentId) {
    const parent = nodeById.get(parentId);
    if (!parent) {
      break;
    }

    const allChildrenDone =
      parent.children.length > 0 &&
      parent.children.every((child) => (updates.get(child.id) ?? child.status) === 'done');
    updates.set(parent.id, allChildrenDone ? 'done' : 'todo');
    parentId = parent.parentId ?? null;
  }

  return Object.fromEntries(
    [...updates.entries()].filter(
      ([nodeId, status]) => nodeById.get(nodeId)?.status !== status,
    ),
  );
}

function hasStagedAncestor(
  node: StrategyNode,
  nodeById: Map<string, StrategyNode>,
  stagedNodeIdSet: Set<string>,
) {
  let parentId = node.parentId ?? null;

  while (parentId) {
    if (stagedNodeIdSet.has(parentId)) {
      return true;
    }

    parentId = nodeById.get(parentId)?.parentId ?? null;
  }

  return false;
}

interface StagedTaskCard {
  node: StrategyNode;
  category: Category | null;
}

interface CalendarTreeFocusRequest {
  nodeId: string;
  categoryId: number;
  requestId: number;
}

function App() {
  const shouldShowFutureStatePreview =
    new URLSearchParams(window.location.search).get('preview') === 'future-state';

  const {
    categories,
    activeCategory,
    isLoading: isCatLoading,
    error: categoryError,
    fetchCategories,
    setActiveCategory,
  } = useCategoryStore();
  const { activeDestination, setActiveDestination } = useUiStore();
  const nodes = useNodeStore((state) => state.nodes);
  const isNodesLoading = useNodeStore((state) => state.isLoading);
  const nodeError = useNodeStore((state) => state.error);
  const fetchNodes = useNodeStore((state) => state.fetchNodes);
  const fetchAllNodes = useNodeStore((state) => state.fetchAllNodes);
  const addNode = useNodeStore((state) => state.addNode);
  const updateNode = useNodeStore((state) => state.updateNode);
  const updateNodeStatuses = useNodeStore((state) => state.updateNodeStatuses);
  const categoryNodeCache = useNodeStore((state) => state.categoryNodeCache);
  const stagedNodeIds = useNodeStore((state) => state.stagedNodeIds);
  const stageNode = useNodeStore((state) => state.stageNode);
  const toggleStagedNode = useNodeStore((state) => state.toggleStagedNode);
  const removeStagedNode = useNodeStore((state) => state.removeStagedNode);
  const reorderStagedNode = useNodeStore((state) => state.reorderStagedNode);
  const pendingFocusNodeId = useNodeStore((state) => state.pendingFocusNodeId);
  const consumePendingFocusNodeId = useNodeStore((state) => state.consumePendingFocusNodeId);
  const clearError = useNodeStore((state) => state.clearError);
  const runAutoScheduleDraft = useCalendarStore((state) => state.runAutoScheduleDraft);
  const clearScheduleDraft = useCalendarStore((state) => state.clearScheduleDraft);
  const isAutoScheduling = useCalendarStore((state) => state.isScheduling);
  const isPlannerProposing = useCalendarStore((state) => state.isPlannerProposing);
  const schedulePatchProposal = useCalendarStore((state) => state.schedulePatchProposal);
  const scheduleDraftResult = useCalendarStore((state) => state.scheduleDraftResult);
  const plannerProposalError = useCalendarStore((state) => state.plannerProposalError);
  const scheduledTaskIds = useCalendarStore((state) => state.scheduledTaskIds);
  const scheduledTaskIdSet = useMemo(
    () => new Set(scheduledTaskIds),
    [scheduledTaskIds],
  );

  const [isBacklogExpanded, setIsBacklogExpanded] = useState(false);
  const [isArchivedExpanded, setIsArchivedExpanded] = useState(false);
  const [isSidebarCollapsed, setIsSidebarCollapsed] = useState(false);
  const [viewportWidth, setViewportWidth] = useState(() => window.innerWidth);
  const [collapsedPanels, setCollapsedPanels] = useState<Record<PanelKey, boolean>>({
    planning: false,
    ai: false,
    calendar: false,
  });
  const [panelWidths, setPanelWidths] = useState<WorkspacePanelWidths>(loadPanelWidths);
  const [aiWorkspaceTab, setAiWorkspaceTab] = useState<AiWorkspaceTab>('chat');
  const [aiContextTask, setAiContextTask] = useState<StrategyNode | null>(null);
  const [aiWorkspaceLaunch, setAiWorkspaceLaunch] = useState<AiWorkspaceLaunchRequest | null>(null);
  const [calendarTreeFocusRequest, setCalendarTreeFocusRequest] =
    useState<CalendarTreeFocusRequest | null>(null);
  const calendarPointerDropHandlerRef = useRef<CalendarPointerDropHandler | null>(null);
  const calendarTreeFocusRequestIdRef = useRef(0);
  const categoryIds = useMemo(
    () => categories.map((category) => category.id),
    [categories],
  );

  useEffect(() => {
    window.localStorage.setItem(PANEL_WIDTH_STORAGE_KEY, JSON.stringify(panelWidths));
  }, [panelWidths]);

  useEffect(() => {
    fetchCategories();
  }, [fetchCategories]);

  useEffect(() => {
    if (activeDestination === 'category' && activeCategory) {
      void fetchNodes(activeCategory.id);
      return;
    }
    if (activeDestination === 'all') {
      void fetchAllNodes(categoryIds);
    }
  }, [activeCategory, activeDestination, categoryIds, fetchAllNodes, fetchNodes]);

  useEffect(() => {
    const handleResize = () => {
      setViewportWidth(window.innerWidth);
    };

    window.addEventListener('resize', handleResize);
    return () => {
      window.removeEventListener('resize', handleResize);
    };
  }, []);

  useEffect(() => {
    if (!nodeError) {
      return;
    }

    const timer = window.setTimeout(() => {
      clearError();
    }, 3600);

    return () => {
      window.clearTimeout(timer);
    };
  }, [clearError, nodeError]);

  const flatNodes = useMemo(() => flattenNodes(nodes), [nodes]);
  const cachedFlatNodes = useMemo(
    () => Object.values(categoryNodeCache).flatMap(flattenNodes),
    [categoryNodeCache],
  );
  const backlogNodeIds = useMemo(
    () => new Set(flatNodes.filter(isBacklogTreeNode).map((node) => node.id)),
    [flatNodes],
  );
  const archivedNodeIds = useMemo(
    () => new Set(flatNodes.filter(isArchivedNode).map((node) => node.id)),
    [flatNodes],
  );
  const activeNodes = useMemo(() => nodes.filter(isActiveTreeNode), [nodes]);
  const backlogNodes = useMemo(
    () =>
      flatNodes.filter(
        (node) =>
          isBacklogTreeNode(node) && (!node.parentId || !backlogNodeIds.has(node.parentId)),
      ),
    [backlogNodeIds, flatNodes],
  );
  const archivedNodes = useMemo(
    () =>
      flatNodes.filter(
        (node) =>
          isArchivedNode(node) && (!node.parentId || !archivedNodeIds.has(node.parentId)),
      ),
    [archivedNodeIds, flatNodes],
  );
  const categoryById = useMemo(
    () => new Map(categories.map((category) => [category.id, category])),
    [categories],
  );
  const nodeById = useMemo(
    () => new Map(cachedFlatNodes.map((node) => [node.id, node])),
    [cachedFlatNodes],
  );
  const stagedNodeIdSet = useMemo(() => new Set(stagedNodeIds), [stagedNodeIds]);
  const stagedTasks: StagedTaskCard[] = useMemo(
    () =>
      stagedNodeIds
        .map((id) => nodeById.get(id) ?? null)
        .filter((node): node is StrategyNode => node !== null)
        .filter((node) => !hasStagedAncestor(node, nodeById, stagedNodeIdSet))
        .map((node) => ({
          node,
          category: categoryById.get(node.categoryId) ?? null,
        })),
    [categoryById, nodeById, stagedNodeIdSet, stagedNodeIds],
  );
  const stagedTaskKey = stagedTasks.map(({ node }) => node.id).join(',');
  const stagedEstimateMinutes = useMemo(() => {
    const sumTree = (node: StrategyNode): number =>
      (node.timeEstimateMinutes ?? 0)
      + node.children.reduce((total, child) => total + sumTree(child), 0);
    return stagedTasks.reduce((total, { node }) => total + sumTree(node), 0);
  }, [stagedTasks]);

  useEffect(() => {
    clearScheduleDraft();
  }, [clearScheduleDraft, stagedTaskKey]);

  const handleStatusChange = useCallback(async (id: string, done: boolean) => {
    const currentCache = useNodeStore.getState().categoryNodeCache;
    const currentRootNodes = Object.values(currentCache).flat();
    const fallbackRootNodes = useNodeStore.getState().nodes;
    const cascadeUpdates = buildCompletionCascadeUpdates(
      id,
      done,
      currentRootNodes.length > 0 ? currentRootNodes : fallbackRootNodes,
    );
    try {
      await updateNodeStatuses(cascadeUpdates);
      return true;
    } catch {
      return false;
    }
  }, [updateNodeStatuses]);

  const handleTitleChange = useCallback((id: string, title: string) => {
    const currentState = useNodeStore.getState();
    const node = Object.values(currentState.categoryNodeCache)
      .flatMap(flattenNodes)
      .find((entry) => entry.id === id)
      ?? flattenNodes(currentState.nodes).find((entry) => entry.id === id);
    updateNode(id, {
      title,
      emoji: suggestEmoji(title, node?.type ?? 'task'),
    });
  }, [updateNode]);

  const handleAddNode = useCallback(() => {
    if (activeDestination === 'category' && activeCategory) {
      addNode({
        title: 'New Task',
        categoryId: activeCategory.id,
        nodeType: 'task',
      });
    }
  }, [activeCategory, activeDestination, addNode]);

  const registerCalendarPointerDrop = useCallback(
    (handler: CalendarPointerDropHandler | null) => {
      calendarPointerDropHandlerRef.current = handler;
    },
    [],
  );

  const handleCalendarPointerDrop = useCallback(
    (payload: NodeDragPayload, clientX: number, clientY: number) =>
      calendarPointerDropHandlerRef.current?.(payload, clientX, clientY) ?? false,
    [],
  );

  const handleCalendarTaskFocus = useCallback(
    (taskId: string, categoryId: number) => {
      const category = categoryById.get(categoryId);
      if (category) {
        setActiveCategory(category);
      }
      setActiveDestination('category');
      setCollapsedPanels((current) =>
        current.planning ? { ...current, planning: false } : current,
      );
      calendarTreeFocusRequestIdRef.current += 1;
      setCalendarTreeFocusRequest({
        nodeId: taskId,
        categoryId,
        requestId: calendarTreeFocusRequestIdRef.current,
      });
    },
    [categoryById, setActiveCategory, setActiveDestination],
  );

  const handleCalendarTreeFocusHandled = useCallback((requestId: number) => {
    setCalendarTreeFocusRequest((current) =>
      current?.requestId === requestId ? null : current,
    );
  }, []);

  const handleOpenCalendarFromAgenda = useCallback(() => {
    useCalendarStore.getState().setViewMode('day');
    setActiveDestination('category');
    setCollapsedPanels((current) =>
      current.calendar ? { ...current, calendar: false } : current,
    );
  }, [setActiveDestination]);

  const handleAutoScheduleSelected = useCallback(() => {
    const taskIds = stagedTasks.map(({ node }) => node.id);
    if (taskIds.length === 0) {
      clearScheduleDraft();
      return;
    }
    void runAutoScheduleDraft({ taskIds }).catch(() => undefined);
  }, [clearScheduleDraft, runAutoScheduleDraft, stagedTasks]);

  const handleScheduleProposalAction = useCallback((
    proposalId: string,
    action: 'approve' | 'edit' | 'reject',
  ) => {
    setCollapsedPanels((current) => ({ ...current, ai: false }));
    setAiWorkspaceTab('chat');
    setAiWorkspaceLaunch({
      requestId: `schedule-action-${Date.now()}-${proposalId}-${action}`,
      source: 'schedule_proposal_action',
      stagedNodeIds: stagedTasks.map(({ node }) => node.id),
      proposalId,
      action,
    });
  }, [stagedTasks]);

  if (isCatLoading && !activeCategory) {
    return (
      <div className="app-loading" aria-live="polite" aria-busy="true">
        Loading workspaces...
      </div>
    );
  }

  if (categoryError && !activeCategory) {
    return (
      <div className="app-startup-recovery" role="alert">
        <div className="app-startup-recovery-card">
          <p className="app-startup-recovery-eyebrow">Workspace connection</p>
          <h1>Workspaces did not load yet</h1>
          <p>
            {categoryError} This can happen if the frontend opens before the backend
            finishes restarting.
          </p>
          <button
            type="button"
            className="app-startup-recovery-button"
            onClick={() => {
              void fetchCategories();
            }}
          >
            Retry loading workspaces
          </button>
        </div>
      </div>
    );
  }

  if (shouldShowFutureStatePreview) {
    return (
      <Suspense fallback={<main aria-live="polite">Loading preview…</main>}>
        <FutureStatePreview />
      </Suspense>
    );
  }

  const currentAccentColor =
    activeDestination === 'all' ? '#6366f1' : activeCategory?.accentColor ?? '#6366f1';
  const isHorizontalPanelLayout = viewportWidth > 767;
  const isResizablePanelLayout = viewportWidth >= 1280;
  const appShellStyle = isResizablePanelLayout
    ? {
        gridTemplateColumns: getDesktopShellColumns(
          isSidebarCollapsed,
          collapsedPanels,
          panelWidths,
        ),
      }
    : undefined;

  const togglePanelCollapse = (panelKey: PanelKey) => {
    setCollapsedPanels((current) => ({
      ...current,
      [panelKey]: !current[panelKey],
    }));
  };

  const resizePanelBy = (boundary: ResizeBoundary, delta: number) => {
    setPanelWidths((current) => ({
      ...current,
      planning:
        boundary === 'planning-ai'
          ? clampPanelWidth(current.planning + delta, 330, 620)
          : current.planning,
      ai:
        boundary === 'ai-calendar'
          ? clampPanelWidth(current.ai + delta, 360, 720)
          : current.ai,
    }));
  };

  const handlePanelResizeStart = (
    boundary: ResizeBoundary,
    event: ReactPointerEvent<HTMLDivElement>,
  ) => {
    event.preventDefault();
    const startX = event.clientX;
    const startWidths = panelWidths;
    const handlePointerMove = (moveEvent: PointerEvent) => {
      const delta = moveEvent.clientX - startX;
      setPanelWidths({
        planning:
          boundary === 'planning-ai'
            ? clampPanelWidth(startWidths.planning + delta, 330, 620)
            : startWidths.planning,
        ai:
          boundary === 'ai-calendar'
            ? clampPanelWidth(startWidths.ai + delta, 360, 720)
            : startWidths.ai,
      });
    };
    const stopResizing = () => {
      document.body.classList.remove('is-resizing-workspace');
      window.removeEventListener('pointermove', handlePointerMove);
      window.removeEventListener('pointerup', stopResizing);
      window.removeEventListener('pointercancel', stopResizing);
    };
    document.body.classList.add('is-resizing-workspace');
    window.addEventListener('pointermove', handlePointerMove);
    window.addEventListener('pointerup', stopResizing, { once: true });
    window.addEventListener('pointercancel', stopResizing, { once: true });
  };

  const handlePanelResizerKeyDown = (
    boundary: ResizeBoundary,
    event: ReactKeyboardEvent<HTMLDivElement>,
  ) => {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
    event.preventDefault();
    resizePanelBy(boundary, event.key === 'ArrowLeft' ? -16 : 16);
  };

  const renderPanelChrome = (
    panelKey: PanelKey,
    panelTitle: string,
    content: ReactNode,
  ) => {
    const isCollapsed = isHorizontalPanelLayout && collapsedPanels[panelKey];
    const dock = panelKey === 'calendar' ? 'left' : 'right';

    if (isCollapsed) {
      return (
        <button
          type="button"
          className={`workspace-panel-slot workspace-panel-rail is-collapsed panel-${panelKey}`}
          onClick={() => togglePanelCollapse(panelKey)}
          aria-label={`Expand ${panelTitle}`}
          title={`Expand ${panelTitle}`}
        >
          <span className="workspace-panel-collapsed-label">
            <RailIcon panelKey={panelKey} />
          </span>
        </button>
      );
    }

    return (
      <section className={`workspace-panel-slot panel-${panelKey}`}>
        {isHorizontalPanelLayout && (
          <button
            type="button"
            className="workspace-panel-toggle"
            onClick={() => togglePanelCollapse(panelKey)}
            aria-expanded={true}
            aria-label={`Collapse ${panelTitle}`}
            title={`Collapse ${panelTitle}`}
          >
            <PanelToggleIcon collapsed={false} dock={dock} />
          </button>
        )}
        <div className="workspace-panel-slot-content">{content}</div>
      </section>
    );
  };

  const renderPanelBoundary = (boundary: ResizeBoundary, label: string) => {
    if (!isResizablePanelLayout) return null;

    return (
      <div
        className="workspace-panel-resizer"
        role="separator"
        tabIndex={0}
        aria-label={label}
        aria-orientation="vertical"
        onPointerDown={(event) => handlePanelResizeStart(boundary, event)}
        onKeyDown={(event) => handlePanelResizerKeyDown(boundary, event)}
      />
    );
  };

  return (
    <div className="app-root">
      <AppSidebar
        isCollapsed={isSidebarCollapsed}
        onToggleCollapse={() => setIsSidebarCollapsed((value) => !value)}
      />

      <main className="app-main" aria-label="Workspace content">
        {nodeError && (
          <div className="app-error-alert" role="alert">
            <span>Warning: {nodeError}</span>
            <button
              type="button"
              className="app-error-dismiss"
              onClick={clearError}
              aria-label="Dismiss warning"
            >
              x
            </button>
          </div>
        )}

        {categoryError && activeCategory && (
          <div className="app-error-alert" role="alert">
            <span>Warning: {categoryError}</span>
            <button
              type="button"
              className="app-error-retry"
              onClick={() => {
                void fetchCategories();
              }}
            >
              Retry
            </button>
          </div>
        )}

        {activeDestination === 'my-day' ? (
          <MyDayAgenda
            taskLookup={nodeById}
            focusTasks={stagedTasks}
            onTaskStatusChange={handleStatusChange}
            onTaskFocus={handleCalendarTaskFocus}
            onOpenCalendar={handleOpenCalendarFromAgenda}
          />
        ) : (
        <div className="app-shell" style={appShellStyle} data-testid="app-shell">
          {renderPanelChrome(
            'planning',
            'Planning workspace',
            <PlanningWorkspacePanel
              activeDestination={activeDestination}
              activeCategory={activeCategory}
              categoryById={categoryById}
              isNodesLoading={isNodesLoading}
              activeNodes={activeNodes}
              backlogNodes={backlogNodes}
              archivedNodes={archivedNodes}
              stagedNodeIds={stagedNodeIds}
              scheduledNodeIdSet={scheduledTaskIdSet}
              isBacklogExpanded={isBacklogExpanded}
              onBacklogToggle={() => setIsBacklogExpanded((value) => !value)}
              isArchivedExpanded={isArchivedExpanded}
              onArchivedToggle={() => setIsArchivedExpanded((value) => !value)}
              onAddNode={handleAddNode}
              onStatusChange={handleStatusChange}
              onTitleChange={handleTitleChange}
              onStageNode={stageNode}
              onToggleStageNode={toggleStagedNode}
              onSchedulePointerDrop={handleCalendarPointerDrop}
              pendingFocusNodeId={pendingFocusNodeId}
              onAutoFocusHandled={consumePendingFocusNodeId}
              calendarFocusRequest={calendarTreeFocusRequest}
              onCalendarFocusHandled={handleCalendarTreeFocusHandled}
              onAiContextTaskChange={setAiContextTask}
            />,
          )}

          {renderPanelBoundary('planning-ai', 'Resize planning and AI workspaces')}

          {renderPanelChrome(
            'ai',
            'AI workspace',
            <section className="ai-workspace-panel" aria-labelledby="ai-workspace-heading">
              <header className="ai-workspace-header">
                <h2 id="ai-workspace-heading">AI workspace</h2>
                <div className="ai-workspace-tabs" role="tablist" aria-label="AI workspace views">
                  <button
                    type="button"
                    role="tab"
                    id="ai-chat-tab"
                    aria-selected={aiWorkspaceTab === 'chat'}
                    aria-controls="ai-chat-view"
                    className={aiWorkspaceTab === 'chat' ? 'is-active' : ''}
                    onClick={() => setAiWorkspaceTab('chat')}
                  >
                    Chat
                  </button>
                  <button
                    type="button"
                    role="tab"
                    id="ai-selected-tab"
                    aria-selected={aiWorkspaceTab === 'selected'}
                    aria-controls="ai-selected-view"
                    className={aiWorkspaceTab === 'selected' ? 'is-active' : ''}
                    onClick={() => setAiWorkspaceTab('selected')}
                  >
                    Selected tasks
                    <span>{stagedNodeIds.length}</span>
                  </button>
                </div>
              </header>

              <div className="ai-workspace-views">
                <div
                  id="ai-chat-view"
                  role="tabpanel"
                  aria-labelledby="ai-chat-tab"
                  className="ai-workspace-view"
                  hidden={aiWorkspaceTab !== 'chat'}
                >
                  {activeDestination === 'all' || activeCategory ? (
                    <StrategistAiCard
                      embedded
                      activeCategory={activeDestination === 'all' ? null : activeCategory}
                      currentTask={aiContextTask}
                      stagedNodeIds={stagedTasks.map(({ node }) => node.id)}
                      isExpanded={false}
                      onToggleExpanded={() => undefined}
                      onClose={() => undefined}
                      launchRequest={aiWorkspaceLaunch}
                      onLaunchHandled={(requestId) => {
                        setAiWorkspaceLaunch((current) =>
                          current?.requestId === requestId ? null : current,
                        );
                      }}
                    />
                  ) : (
                    <div className="ai-workspace-empty">Create a workspace to start chatting.</div>
                  )}
                </div>

                <div
                  id="ai-selected-view"
                  role="tabpanel"
                  aria-labelledby="ai-selected-tab"
                  className="ai-workspace-view"
                  hidden={aiWorkspaceTab !== 'selected'}
                >
                  <SelectedTasksPanel
                    embedded
                    activeDestination={activeDestination}
                    stagedTasks={stagedTasks}
                    stagedTaskCount={stagedNodeIds.length}
                    stagedEstimateMinutes={stagedEstimateMinutes}
                    scheduledTaskIdSet={scheduledTaskIdSet}
                    scheduleDraftResult={scheduleDraftResult}
                    isNodesLoading={isNodesLoading}
                    onStageDroppedTask={stageNode}
                    onRemoveSelectedTask={removeStagedNode}
                    onReorderSelectedTask={reorderStagedNode}
                    onAutoScheduleSelected={handleAutoScheduleSelected}
                    isAutoScheduling={isAutoScheduling}
                    isPlannerProposing={isPlannerProposing}
                    isPlannerProposalActionPending={
                      aiWorkspaceLaunch?.source === 'schedule_proposal_action'
                    }
                    plannerProposal={schedulePatchProposal}
                    plannerProposalError={plannerProposalError}
                    onPlannerProposalAction={handleScheduleProposalAction}
                  />
                </div>
              </div>
            </section>,
          )}

          {renderPanelBoundary('ai-calendar', 'Resize AI workspace and calendar')}

          {renderPanelChrome(
            'calendar',
            'Shared calendar',
            <section
              className="workspace-panel workspace-panel-calendar"
              aria-label="Shared calendar"
            >
              <CalendarPanel
                accentColor={currentAccentColor}
                headingId="workspace-calendar-heading"
                taskLookup={nodeById}
                onTaskStatusChange={handleStatusChange}
                onTaskFocus={handleCalendarTaskFocus}
                onRegisterPointerDrop={registerCalendarPointerDrop}
              />
            </section>,
          )}
        </div>
        )}
      </main>
    </div>
  );
}

export default App;
