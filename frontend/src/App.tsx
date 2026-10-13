import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type {
  KeyboardEvent as ReactKeyboardEvent,
  CSSProperties,
  PointerEvent as ReactPointerEvent,
  ReactNode,
} from 'react';
import './styles/globals.css';
import './App.css';
import './styles/workspace-refinement.css';
import './styles/mobile-workspace.css';
import { FloatingChat } from './components/ai/FloatingChat';
import { useCompanionCount } from './components/ai/useCompanionCount';
import { TaskComposer } from './components/planning/TaskComposer';
import { SettingsDialog } from './components/settings/SettingsDialog';
import { CalendarPanel } from './components/calendar/CalendarPanel';
import type { CalendarPointerDropHandler } from './components/calendar/CalendarPanel';
import { AppSidebar } from './components/sidebar/AppSidebar';
import { MobileNavigation } from './components/sidebar/MobileNavigation';
import { useMediaQuery } from './hooks/useMediaQuery';
import { useVisualViewport } from './hooks/useVisualViewport';
import { PlanningWorkspacePanel } from './components/planning/PlanningWorkspacePanel';
import { SelectedTasksPanel } from './components/selected/SelectedTasksPanel';
import { MyDayAgenda } from './components/agenda/MyDayAgenda';
import { useCategoryStore } from './store/useCategoryStore';
import { useCalendarStore } from './store/useCalendarStore';
import { useNodeStore } from './store/useNodeStore';
import { useUiStore } from './store/useUiStore';
import { refreshWorkspace, useWorkspaceStore } from './store/useWorkspaceStore';
import type { NodeDragPayload, NodeStatus, StrategyNode } from './types/node';
import type { Category } from './types/category';

const StrategistAiCard = lazy(() => import('./components/planning/StrategistAiCard').then(module => ({ default: module.StrategistAiCard })));
const CompanionPanel = lazy(() => import('./components/ai/CompanionPanel').then(module => ({ default: module.CompanionPanel })));
const HabitsPage = lazy(() => import('./components/habits/HabitsPage').then(module => ({ default: module.HabitsPage })));
import { suggestEmoji } from './lib/emoji';
import { isArchivedNode, isActiveTreeNode, isBacklogTreeNode } from './lib/nodeState';

const FutureStatePreview = lazy(() =>
  import('./components/future/FutureStatePreview').then((module) => ({
    default: module.FutureStatePreview,
  })),
);

export interface AiWorkspaceLaunchRequest {
  requestId: string;
  source: 'custom_schedule' | 'schedule_proposal_action' | 'companion';
  prompt?: string;
  stagedNodeIds: string[];
  proposalId?: string;
  action?: 'approve' | 'edit' | 'reject';
}

type PanelKey = 'planning' | 'ai' | 'calendar';
type AiWorkspaceTab = 'chat' | 'selected' | 'companion';
type ResizeBoundary = 'planning-ai' | 'ai-calendar';

interface WorkspacePanelWidths {
  planning: number;
  ai: number;
}

const DEFAULT_PANEL_WIDTHS: WorkspacePanelWidths = { planning: 300, ai: 380 };
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
      // Carry the previous default forward to the compact tree; retain custom widths.
      planning: clampPanelWidth(parsed.planning === 350 ? 300 : parsed.planning ?? DEFAULT_PANEL_WIDTHS.planning, 270, 620),
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

function flattenNodes(nodes: StrategyNode[]): StrategyNode[] {
  return nodes.flatMap((node) => [node, ...flattenNodes(node.children)]);
}

function buildCompletionCascadeUpdates(
  id: string,
  done: boolean,
  nodes: StrategyNode[],
  scopeIds?: ReadonlySet<string>,
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
    node.children.forEach(setSubtreeStatus);
    if (!scopeIds || scopeIds.has(node.id)) {
      updates.set(node.id, scopeIds && node.children.length
        ? node.children.every(child => (updates.get(child.id) ?? child.status) === 'done') ? 'done' : 'todo'
        : targetStatus);
    }
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
  useVisualViewport();
  const isCompactPanelLayout = useMediaQuery('(max-width: 1023px)');
  const { ready: workspaceReady, legacy: legacyLoading, error: workspaceError } = useWorkspaceStore();
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
  const { activeDestination, setActiveDestination, isSettingsOpen } = useUiStore();
  const nodes = useNodeStore((state) => state.nodes);
  const isNodesLoading = useNodeStore((state) => state.isLoading);
  const nodeError = useNodeStore((state) => state.error);
  const companionCount = useCompanionCount(workspaceReady || (legacyLoading && !isCatLoading && !isNodesLoading && categories.length > 0));
  const fetchNodes = useNodeStore((state) => state.fetchNodes);
  const fetchAllNodes = useNodeStore((state) => state.fetchAllNodes);
  const [taskComposer, setTaskComposer] = useState<{ categoryId: number; day?: string } | null>(null);
  const [planningDay, setPlanningDay] = useState<string | null>(null);
  const [calendarFocusDay, setCalendarFocusDay] = useState<string | null>(null);
  const calendarViewMode = useCalendarStore(state => state.viewMode);
  const handlePlanningDayChange = useCallback((day: string | null) => {
    setPlanningDay(day);
    if (day) {
      setCalendarFocusDay(day);
    }
  }, []);
  const handleCalendarDayChange = useCallback((day: string | null) => {
    setCalendarFocusDay(day);
    if (planningDay !== null) handlePlanningDayChange(day);
  }, [planningDay, handlePlanningDayChange]);
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
  const [collapsedPanels, setCollapsedPanels] = useState<Record<PanelKey, boolean>>({
    planning: false,
    ai: false,
    calendar: false,
  });
  const [panelWidths, setPanelWidths] = useState<WorkspacePanelWidths>(loadPanelWidths);
  const [responsivePanel, setResponsivePanel] = useState<PanelKey>('planning');
  const [chatOpen, setChatOpen] = useState(false);
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
  const measuredFirstPaint = useRef(false);
  const resizeCleanup = useRef<(() => void) | null>(null);
  useEffect(() => () => resizeCleanup.current?.(), []);
  useEffect(() => {
    if ((!workspaceReady && !legacyLoading) || isNodesLoading || categories.length === 0) return;
    const frame = requestAnimationFrame(() => {
      if (!measuredFirstPaint.current) {
        measuredFirstPaint.current = true;
        performance.measure('drasil:reload-to-tasks', { start: 0, end: performance.now() });
      }
      const switches = performance.getEntriesByName('drasil:category-switch-start');
      const latest = switches.at(-1);
      if (latest) {
        performance.measure('drasil:category-to-tasks', { start: latest.startTime, end: performance.now() });
        performance.clearMarks('drasil:category-switch-start');
      }
    });
    return () => cancelAnimationFrame(frame);
  }, [activeCategory?.id, categories.length, isNodesLoading, legacyLoading, nodes, workspaceReady]);

  useEffect(() => {
    if (document.body.classList.contains('is-resizing-workspace')) return;
    window.localStorage.setItem(PANEL_WIDTH_STORAGE_KEY, JSON.stringify(panelWidths));
  }, [panelWidths]);

  useEffect(() => {
    if (legacyLoading) void fetchCategories();
    else void refreshWorkspace();
  }, [fetchCategories, legacyLoading]);

  useEffect(() => {
    if (!legacyLoading && !workspaceReady) return;
    if (activeDestination === 'category' && activeCategory) {
      void fetchNodes(activeCategory.id, { force: false });
      return;
    }
    if (activeDestination === 'all') {
      void fetchAllNodes(categoryIds, { force: false });
    }
  }, [activeCategory, activeDestination, categoryIds, fetchAllNodes, fetchNodes, legacyLoading, workspaceReady]);

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

  const handleStatusChange = useCallback(async (id: string, done: boolean, scopeIds?: string[]) => {
    const currentCache = useNodeStore.getState().categoryNodeCache;
    const currentRootNodes = Object.values(currentCache).flat();
    const fallbackRootNodes = useNodeStore.getState().nodes;
    const cascadeUpdates = buildCompletionCascadeUpdates(
      id,
      done,
      currentRootNodes.length > 0 ? currentRootNodes : fallbackRootNodes,
      scopeIds ? new Set(scopeIds) : undefined,
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
    if (activeCategory) setTaskComposer({ categoryId: activeCategory.id });
  }, [activeCategory]);

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
      setResponsivePanel('planning');
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
    setResponsivePanel('calendar');
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
    setChatOpen(true);
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
  const isHorizontalPanelLayout = !isCompactPanelLayout;
  const isResizablePanelLayout = !isCompactPanelLayout;
  const appShellStyle = {
    '--preferred-tree-panel-width': `${panelWidths.planning}px`,
    '--preferred-calendar-panel-width': `${panelWidths.ai}px`,
  } as CSSProperties;

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
          ? clampPanelWidth(current.planning + delta, 270, 620)
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
    if (event.button !== 0) return;
    resizeCleanup.current?.();
    event.preventDefault();
    const startX = event.clientX;
    const startWidths = panelWidths;
    const pointerId = event.pointerId;
    let frame = 0;
    let latestWidths = startWidths;
    const handlePointerMove = (moveEvent: PointerEvent) => {
      if (moveEvent.pointerId !== pointerId) return;
      const delta = moveEvent.clientX - startX;
      latestWidths = {
        planning:
          boundary === 'planning-ai'
            ? clampPanelWidth(startWidths.planning + delta, 270, 620)
            : startWidths.planning,
        ai:
          boundary === 'ai-calendar'
            ? clampPanelWidth(startWidths.ai - delta, 360, 720)
            : startWidths.ai,
      };
      if (!frame) frame = requestAnimationFrame(() => { frame = 0; setPanelWidths(latestWidths); });
    };
    const stopResizing = (endEvent?: PointerEvent) => {
      if (endEvent && endEvent.pointerId !== pointerId) return;
      cancelAnimationFrame(frame);
      if (endEvent) setPanelWidths(latestWidths);
      window.localStorage.setItem(PANEL_WIDTH_STORAGE_KEY, JSON.stringify(latestWidths));
      document.body.classList.remove('is-resizing-workspace');
      window.removeEventListener('pointermove', handlePointerMove);
      window.removeEventListener('pointerup', stopResizing);
      window.removeEventListener('pointercancel', stopResizing);
      resizeCleanup.current = null;
    };
    resizeCleanup.current = stopResizing;
    document.body.classList.add('is-resizing-workspace');
    window.addEventListener('pointermove', handlePointerMove);
    window.addEventListener('pointerup', stopResizing);
    window.addEventListener('pointercancel', stopResizing);
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
    const isCollapsed = isHorizontalPanelLayout && panelKey === 'calendar' && collapsedPanels[panelKey];
    const isResponsivePanelActive = !isCompactPanelLayout || responsivePanel === panelKey;
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
      <section
        className={`workspace-panel-slot panel-${panelKey}${
          isResponsivePanelActive ? ' is-responsive-active' : ''
        }`}
        aria-hidden={isCompactPanelLayout && !isResponsivePanelActive ? true : undefined}
      >
        {isHorizontalPanelLayout && panelKey === 'calendar' && (
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
        data-resize-boundary={boundary}
        aria-orientation="vertical"
        onPointerDown={(event) => handlePanelResizeStart(boundary, event)}
        onKeyDown={(event) => handlePanelResizerKeyDown(boundary, event)}
      />
    );
  };

  return (
    <div className="app-root">
      {!isCompactPanelLayout && <AppSidebar
        isCollapsed={isSidebarCollapsed}
        onToggleCollapse={() => setIsSidebarCollapsed((value) => !value)}
      />}

      <main className="app-main" aria-label="Workspace content">
        {isCompactPanelLayout && <MobileNavigation />}
        {workspaceError && (
          <div className="app-error-alert" role="alert">
            <span>Could not refresh workspace: {workspaceError}</span>
            <button type="button" className="app-error-retry" onClick={() => void refreshWorkspace()}>Retry</button>
          </div>
        )}
        {nodeError && (
          <div className="app-error-alert" role="alert">
            <span>Warning: {nodeError}</span>
            {(nodeError === 'Failed to load nodes.' || nodeError === 'Failed to load all workspace tasks.') && (
              <button
                type="button"
                className="app-error-retry"
                onClick={() => {
                  if (activeDestination === 'category' && activeCategory) {
                    void fetchNodes(activeCategory.id);
                  } else {
                    void fetchAllNodes(categoryIds);
                  }
                }}
              >
                Retry
              </button>
            )}
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

        {activeDestination === 'habits' ? (
          <Suspense fallback={<p role="status">Loading habits…</p>}><HabitsPage /></Suspense>
        ) : activeDestination === 'my-day' ? (
          <MyDayAgenda
            taskLookup={nodeById}
            focusTasks={stagedTasks}
            onTaskStatusChange={handleStatusChange}
            onTaskFocus={handleCalendarTaskFocus}
            onOpenCalendar={handleOpenCalendarFromAgenda}
          />
        ) : (
        <>
        <nav className="responsive-workspace-nav" aria-label="Workspace views">
          <button
            type="button"
            className={responsivePanel === 'planning' ? 'is-active' : ''}
            aria-pressed={responsivePanel === 'planning'}
            onClick={() => setResponsivePanel('planning')}
          >
            Plan
          </button>
          <button
            type="button"
            className={responsivePanel === 'calendar' ? 'is-active' : ''}
            aria-pressed={responsivePanel === 'calendar'}
            onClick={() => setResponsivePanel('calendar')}
          >
            Calendar
          </button>
        </nav>
        <div className={`app-shell app-shell-floating${calendarViewMode !== 'day' ? ' has-expanded-calendar' : ''}`} style={appShellStyle} data-testid="app-shell">
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
              planningDay={planningDay}
              onPlanningDayChange={handlePlanningDayChange}
              isCalendarOpen={isCompactPanelLayout ? responsivePanel === 'calendar' : !collapsedPanels.calendar}
              onCalendarToggle={() => {
                if (isCompactPanelLayout) setResponsivePanel(current => current === 'calendar' ? 'planning' : 'calendar');
                else togglePanelCollapse('calendar');
              }}
              onAddPlannedTask={day => {
                const categoryId = activeCategory?.id ?? categories[0]?.id;
                if (categoryId !== undefined) setTaskComposer({ categoryId, day });
              }}
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

          {renderPanelBoundary('planning-ai', 'Resize task repository')}
          {renderPanelBoundary('ai-calendar', 'Resize calendar panel')}

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
                focusedDay={calendarFocusDay}
                onFocusedDayChange={handleCalendarDayChange}
                taskLookup={nodeById}
                onAddTask={day => setTaskComposer({ categoryId: activeCategory?.id ?? categories[0]?.id, day })}
                onTaskStatusChange={handleStatusChange}
                onTaskFocus={handleCalendarTaskFocus}
                onRegisterPointerDrop={registerCalendarPointerDrop}
              />
            </section>,
          )}
        </div>
        </>
        )}
      </main>
      <FloatingChat open={chatOpen} onOpenChange={setChatOpen} count={stagedNodeIds.length} companionCount={companionCount}>
            <Suspense fallback={<p role="status">Loading chat…</p>}>
            <section className="ai-workspace-panel" aria-label="Chat and selected tasks">
              <header className="ai-workspace-header">
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
                  <button type="button" role="tab" id="ai-companion-tab" aria-controls="ai-companion-view"
                    aria-selected={aiWorkspaceTab === 'companion'} className={aiWorkspaceTab === 'companion' ? 'is-active' : ''}
                    onClick={() => setAiWorkspaceTab('companion')}>Companion{companionCount > 0 && <span>{companionCount}</span>}</button>
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
                {aiWorkspaceTab === 'companion' && <div id="ai-companion-view" role="tabpanel" aria-labelledby="ai-companion-tab" className="ai-workspace-view">
                  <CompanionPanel onPlan={prompt => {
                    setAiWorkspaceLaunch({ requestId: crypto.randomUUID(), source: 'companion', stagedNodeIds: [], prompt });
                    setAiWorkspaceTab('chat');
                  }} />
                </div>}
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
            </section>
            </Suspense>
      </FloatingChat>
      {isSettingsOpen && <SettingsDialog />}
      {taskComposer && <TaskComposer categories={categories} categoryId={taskComposer.categoryId}
        initialDay={taskComposer.day} onClose={() => setTaskComposer(null)} />}
    </div>
  );
}

export default App;
