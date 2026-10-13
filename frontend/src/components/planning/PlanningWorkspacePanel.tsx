import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { TaskPageDialog } from './TaskPageDialog';
import { WeeklyPlanningBoard } from './WeeklyPlanningBoard';
import { createPortal } from 'react-dom';
import type {
  ChangeEvent,
  CSSProperties,
  KeyboardEvent as ReactKeyboardEvent,
  PointerEvent as ReactPointerEvent,
} from 'react';
import type { Category } from '../../types/category';
import type {
  NodeDragPayload,
  NodeImageAttachment,
  NodePriority,
  StrategyNode,
} from '../../types/node';
import type { AppDestination } from '../../store/useUiStore';
import { useCategoryStore } from '../../store/useCategoryStore';
import { useNodeStore } from '../../store/useNodeStore';
import { useMediaQuery } from '../../hooks/useMediaQuery';
import { countVisibleTreeNodes, isArchivedNode, visibleChildrenForLane } from '../../lib/nodeState';
import { WorkspaceIcon } from '../../lib/workspaceIcons';
import {
  Check,
  Filter,
  Layers3,
  CalendarDays,
  ListTree,
  MoreHorizontal,
  Pencil,
  RefreshCw,
  Search,
  SlidersHorizontal,
  Trash2,
  TriangleAlert,
  X,
} from 'lucide-react';
import { NodeCard } from '../tree/NodeCard';
import { formatRecurrenceSummary } from '../../lib/recurrence';
import './PlanningWorkspacePanel.css';

const ChatGptAuthSettings = lazy(() =>
  import('../settings/ChatGptAuthSettings').then((module) => ({
    default: module.ChatGptAuthSettings,
  })),
);
const RecurrenceRuleDialog = lazy(() =>
  import('../tree/RecurrenceRuleDialog').then((module) => ({
    default: module.RecurrenceRuleDialog,
  })),
);

interface PlanningWorkspacePanelProps {
  activeDestination: AppDestination;
  activeCategory: Category | null;
  categoryById: ReadonlyMap<number, Category>;
  isNodesLoading: boolean;
  activeNodes: StrategyNode[];
  backlogNodes: StrategyNode[];
  archivedNodes: StrategyNode[];
  stagedNodeIds: string[];
  scheduledNodeIdSet: Set<string>;
  isBacklogExpanded: boolean;
  onBacklogToggle: () => void;
  isArchivedExpanded: boolean;
  onArchivedToggle: () => void;
  onAddNode: () => void;
  onAddPlannedTask: (day?: string) => void;
  planningDay: string | null;
  onPlanningDayChange: (day: string | null) => void;
  isCalendarOpen: boolean;
  onCalendarToggle: () => void;
  onStatusChange: (id: string, done: boolean) => void;
  onTitleChange: (id: string, title: string) => void;
  onStageNode: (id: string) => void;
  onToggleStageNode: (id: string) => void;
  onSchedulePointerDrop?: (payload: NodeDragPayload, clientX: number, clientY: number) => boolean;
  pendingFocusNodeId: string | null;
  onAutoFocusHandled: () => void;
  calendarFocusRequest: { nodeId: string; requestId: number } | null;
  onCalendarFocusHandled: (requestId: number) => void;
  onAiContextTaskChange: (task: StrategyNode | null) => void;
}

type TreeLane = 'active' | 'backlog' | 'archived';
type PointerDropIntent = 'before' | 'inside' | 'after';
type TreeFilterFacet = 'status' | 'schedule' | 'recurrence';
type TreeFilterValue = 'both' | 'positive' | 'negative' | 'none';
type TreeFilterPolarity = 'positive' | 'negative';
type LegacyTreeFilter = 'all' | 'open' | 'scheduled' | 'recurring' | 'completed';
type TreeSort = 'manual' | 'deadline' | 'priority';
type WorkspaceDialogMode = 'rename' | 'delete';

interface TreeFilters {
  status: TreeFilterValue;
  schedule: TreeFilterValue;
  recurrence: TreeFilterValue;
}

interface TreeViewSettings {
  filters: TreeFilters;
  sort: TreeSort;
}

type TreeViewSettingsByWorkspace = Record<string, TreeViewSettings>;

interface VirtualNodeRow {
  kind: 'node';
  key: string;
  node: StrategyNode;
  depth: number;
  lane: TreeLane;
  visibleChildCount: number;
  continuationDepths: number[];
  hasNextSibling: boolean;
}

interface VirtualSectionRow {
  kind: 'section';
  key: 'section-backlog' | 'section-archived';
  section: 'backlog' | 'archived';
  label: string;
  count: number;
  isExpanded: boolean;
}

interface VirtualEmptyRow {
  kind: 'empty';
  key: 'empty-active';
}

interface VirtualWorkspaceRow {
  kind: 'workspace';
  key: string;
  categoryId: number;
  label: string;
  accentColor: string;
}

type VirtualTreeRow =
  | VirtualNodeRow
  | VirtualSectionRow
  | VirtualEmptyRow
  | VirtualWorkspaceRow;

interface PointerTreeDragState {
  source: NodeDragPayload;
  node: StrategyNode;
  x: number;
  y: number;
}

interface PointerDropTarget {
  index: number;
  targetNode: StrategyNode;
  intent: PointerDropIntent;
  root?: boolean;
}

const TREE_ROW_HEIGHT = 72;
const TREE_OVERSCAN_ROWS = 10;
const POINTER_DRAG_THRESHOLD = 6;
const TREE_AUTOSCROLL_EDGE = 44;
const TREE_AUTOSCROLL_STEP = 18;
const TREE_VIEW_SETTINGS_STORAGE_KEY = 'drasil.tree-view-settings.v2';
const LEGACY_TREE_VIEW_SETTINGS_STORAGE_KEY = 'drasil.tree-view-settings.v1';
const DEFAULT_TREE_FILTERS: TreeFilters = {
  status: 'both',
  schedule: 'both',
  recurrence: 'both',
};
const DEFAULT_TREE_VIEW_SETTINGS: TreeViewSettings = {
  filters: DEFAULT_TREE_FILTERS,
  sort: 'manual',
};
const TREE_FILTER_MATRIX: ReadonlyArray<{
  facet: TreeFilterFacet;
  label: string;
  options: ReadonlyArray<readonly [TreeFilterPolarity, string]>;
}> = [
  {
    facet: 'status',
    label: 'Status',
    options: [
      ['positive', 'Open'],
      ['negative', 'Closed'],
    ],
  },
  {
    facet: 'schedule',
    label: 'Schedule',
    options: [
      ['positive', 'Scheduled'],
      ['negative', 'Unscheduled'],
    ],
  },
  {
    facet: 'recurrence',
    label: 'Recurrence',
    options: [
      ['positive', 'Recurring'],
      ['negative', 'One-off'],
    ],
  },
];
const TREE_SORT_OPTIONS: ReadonlyArray<readonly [TreeSort, string]> = [
  ['manual', 'Manual order'],
  ['deadline', 'Deadline'],
  ['priority', 'Priority'],
];

function isTreeFilterValue(value: unknown): value is TreeFilterValue {
  return value === 'both' || value === 'positive' || value === 'negative' || value === 'none';
}

function isTreeSort(value: unknown): value is TreeSort {
  return TREE_SORT_OPTIONS.some(([sort]) => sort === value);
}

function isTreeFilters(value: unknown): value is TreeFilters {
  if (!value || typeof value !== 'object') {
    return false;
  }
  const filters = value as Partial<TreeFilters>;
  return (
    isTreeFilterValue(filters.status)
    && isTreeFilterValue(filters.schedule)
    && isTreeFilterValue(filters.recurrence)
  );
}

function isFilterPolaritySelected(value: TreeFilterValue, polarity: TreeFilterPolarity) {
  return value === 'both' || value === polarity;
}

function filterValueFromSelections(positive: boolean, negative: boolean): TreeFilterValue {
  if (positive && negative) return 'both';
  if (positive) return 'positive';
  if (negative) return 'negative';
  return 'none';
}

function filterValueLabel(facet: TreeFilterFacet, value: TreeFilterValue) {
  if (value === 'both') return 'Any';
  if (value === 'none') return 'None';
  const facetConfig = TREE_FILTER_MATRIX.find((entry) => entry.facet === facet);
  return facetConfig?.options.find(([polarity]) => polarity === value)?.[1] ?? 'Any';
}

function migrateLegacyTreeFilter(filter: LegacyTreeFilter): TreeFilters {
  if (filter === 'open') return { ...DEFAULT_TREE_FILTERS, status: 'positive' };
  if (filter === 'completed') return { ...DEFAULT_TREE_FILTERS, status: 'negative' };
  if (filter === 'scheduled') return { ...DEFAULT_TREE_FILTERS, schedule: 'positive' };
  if (filter === 'recurring') return { ...DEFAULT_TREE_FILTERS, recurrence: 'positive' };
  return DEFAULT_TREE_FILTERS;
}

function isLegacyTreeFilter(value: unknown): value is LegacyTreeFilter {
  return (
    value === 'all'
    || value === 'open'
    || value === 'scheduled'
    || value === 'recurring'
    || value === 'completed'
  );
}

function loadTreeViewSettings(): TreeViewSettingsByWorkspace {
  if (typeof window === 'undefined') {
    return {};
  }

  try {
    const rawSettings = window.localStorage.getItem(TREE_VIEW_SETTINGS_STORAGE_KEY);
    if (rawSettings) {
      const parsed = JSON.parse(rawSettings) as Record<string, Partial<TreeViewSettings>>;
      return Object.fromEntries(
        Object.entries(parsed).flatMap(([key, settings]) =>
          isTreeFilters(settings?.filters) && isTreeSort(settings?.sort)
            ? [[key, { filters: settings.filters, sort: settings.sort }]]
            : [],
        ),
      );
    }

    const legacyRawSettings = window.localStorage.getItem(
      LEGACY_TREE_VIEW_SETTINGS_STORAGE_KEY,
    );
    if (!legacyRawSettings) {
      return {};
    }
    const parsed = JSON.parse(legacyRawSettings) as Record<
      string,
      { filter?: unknown; sort?: unknown }
    >;
    return Object.fromEntries(
      Object.entries(parsed).flatMap(([key, settings]) =>
        isLegacyTreeFilter(settings?.filter) && isTreeSort(settings?.sort)
          ? [[key, { filters: migrateLegacyTreeFilter(settings.filter), sort: settings.sort }]]
          : [],
      ),
    );
  } catch {
    return {};
  }
}

function persistTreeViewSettings(settings: TreeViewSettingsByWorkspace) {
  try {
    window.localStorage.setItem(TREE_VIEW_SETTINGS_STORAGE_KEY, JSON.stringify(settings));
  } catch {
    // Filtering still works for this session when browser storage is unavailable.
  }
}

const utilityContent: Record<
  Exclude<AppDestination, 'all' | 'category'>,
  { eyebrow: string; title: string; body: string; detail: string }
> = {
  habits: {
    eyebrow: 'Habit tracking',
    title: 'Build a steady rhythm',
    body: 'Track daily check-ins and earn rewards in the Habits utility.',
    detail: 'Your task tree and calendar remain available in Workspaces.',
  },
  'my-day': {
    eyebrow: 'Daily focus',
    title: 'My Day sits outside category structure',
    body: 'This lane should stay a focused daily queue rather than pretending to be another workspace.',
    detail: 'Your category tree remains intact while this utility view is active.',
  },
  inbox: {
    eyebrow: 'Capture lane',
    title: 'Inbox remains separate from planning trees',
    body: 'Captured work can land here first, then be routed into the right workspace when it is ready.',
    detail: 'That keeps quick capture from mutating the planning hierarchy.',
  },
  settings: {
    eyebrow: 'System controls',
    title: 'Settings should not become fake task categories',
    body: 'Preferences, integrations, and account controls belong in a utility surface, not in the category database.',
    detail: 'This keeps sidebar destinations consistent with the target shell.',
  },
};

function flattenNodes(nodes: StrategyNode[]): StrategyNode[] {
  return nodes.flatMap((node) => [node, ...flattenNodes(node.children)]);
}

function containsNodeId(node: StrategyNode, id: string): boolean {
  return node.id === id || node.children.some((child) => containsNodeId(child, id));
}

function isExpandedForVirtualTree(
  node: StrategyNode,
  expandedNodeIds: Record<string, boolean>,
  selectedNodeId: string | null,
): boolean {
  return (
    (expandedNodeIds[node.id] ?? true) ||
    (!!selectedNodeId && selectedNodeId !== node.id && containsNodeId(node, selectedNodeId))
  );
}

function collectVirtualNodeRows(
  nodes: StrategyNode[],
  lane: TreeLane,
  expandedNodeIds: Record<string, boolean>,
  selectedNodeId: string | null,
  depth = 0,
  ancestorContinuations: boolean[] = [],
  forceExpanded = false,
): VirtualNodeRow[] {
  return nodes.flatMap((node, index) => {
    const visibleChildren = visibleChildrenForLane(node, lane);
    const isLastSibling = index === nodes.length - 1;
    const row: VirtualNodeRow = {
      kind: 'node',
      key: `${lane}-${node.id}`,
      node,
      depth,
      lane,
      visibleChildCount: visibleChildren.length,
      hasNextSibling: !isLastSibling,
      continuationDepths: ancestorContinuations.flatMap((continues, level) =>
        continues ? [level] : [],
      ),
    };

    if (!forceExpanded && !isExpandedForVirtualTree(node, expandedNodeIds, selectedNodeId)) {
      return [row];
    }

    return [
      row,
      ...collectVirtualNodeRows(
        visibleChildren,
        lane,
        expandedNodeIds,
        selectedNodeId,
        depth + 1,
        depth === 0
          ? ancestorContinuations
          : [...ancestorContinuations, !isLastSibling],
        forceExpanded,
      ),
    ];
  });
}

function deadlineValue(node: StrategyNode) {
  if (!node.deadlineAt) return Number.POSITIVE_INFINITY;
  const value = Date.parse(`${node.deadlineAt}T00:00:00`);
  return Number.isNaN(value) ? Number.POSITIVE_INFINITY : value;
}

function priorityValue(node: StrategyNode) {
  if (node.priority === 'high') return 0;
  if (node.priority === 'medium') return 1;
  return 2;
}

function prepareTreeNodes(
  nodes: StrategyNode[],
  query: string,
  filters: TreeFilters,
  sort: TreeSort,
  scheduledNodeIdSet: Set<string>,
): StrategyNode[] {
  const normalizedQuery = query.trim().toLocaleLowerCase();
  const prepared = nodes.flatMap((node) => {
    const children = prepareTreeNodes(
      node.children,
      query,
      filters,
      sort,
      scheduledNodeIdSet,
    );
    const searchableCopy = [node.title, node.notes, ...node.tags]
      .filter(Boolean)
      .join(' ')
      .toLocaleLowerCase();
    const matchesQuery = !normalizedQuery || searchableCopy.includes(normalizedQuery);
    const isOpen = node.status !== 'done';
    const isScheduled = scheduledNodeIdSet.has(node.id);
    const isRecurring = node.isRecurring;
    const matchesStatus =
      filters.status === 'both'
      || (filters.status === 'positive' && isOpen)
      || (filters.status === 'negative' && !isOpen);
    const matchesSchedule =
      filters.schedule === 'both'
      || (filters.schedule === 'positive' && isScheduled)
      || (filters.schedule === 'negative' && !isScheduled);
    const matchesRecurrence =
      filters.recurrence === 'both'
      || (filters.recurrence === 'positive' && isRecurring)
      || (filters.recurrence === 'negative' && !isRecurring);
    const excludesEverything =
      filters.status === 'none'
      || filters.schedule === 'none'
      || filters.recurrence === 'none';
    const matchesFilter =
      !excludesEverything && matchesStatus && matchesSchedule && matchesRecurrence;

    return matchesQuery && matchesFilter || children.length > 0
      ? [{ ...node, children }]
      : [];
  });

  if (sort === 'manual') return prepared;

  return prepared.toSorted((first, second) => {
    const comparison = sort === 'deadline'
      ? deadlineValue(first) - deadlineValue(second)
      : priorityValue(first) - priorityValue(second);
    return comparison || (first.sortOrder ?? 0) - (second.sortOrder ?? 0);
  });
}

function collectWorkspaceGroupedRows(
  nodes: StrategyNode[],
  lane: TreeLane,
  expandedNodeIds: Record<string, boolean>,
  selectedNodeId: string | null,
  forceExpanded: boolean,
  categoryById: ReadonlyMap<number, Category>,
): VirtualTreeRow[] {
  const nodesByCategory = new Map<number, StrategyNode[]>();
  for (const node of nodes) {
    const categoryNodes = nodesByCategory.get(node.categoryId);
    if (categoryNodes) {
      categoryNodes.push(node);
    } else {
      nodesByCategory.set(node.categoryId, [node]);
    }
  }

  const orderedCategoryIds = [
    ...Array.from(categoryById.keys()).filter((categoryId) => nodesByCategory.has(categoryId)),
    ...Array.from(nodesByCategory.keys()).filter((categoryId) => !categoryById.has(categoryId)),
  ];

  return orderedCategoryIds.flatMap((categoryId) => {
    const category = categoryById.get(categoryId);
    return [
      {
        kind: 'workspace' as const,
        key: `workspace-${lane}-${categoryId}`,
        categoryId,
        label: category?.name ?? 'Workspace',
        accentColor: category?.accentColor ?? '#6366f1',
      },
      ...collectVirtualNodeRows(
        nodesByCategory.get(categoryId) ?? [],
        lane,
        expandedNodeIds,
        selectedNodeId,
        0,
        [],
        forceExpanded,
      ),
    ];
  });
}

function getPointerReorderIndex(
  dropIntent: 'before' | 'after',
  targetNode: StrategyNode,
  source: NodeDragPayload,
): number {
  let targetIndex = (targetNode.sortOrder ?? 0) + (dropIntent === 'after' ? 1 : 0);
  const parentId = targetNode.parentId ?? null;

  if (source.parentId === parentId && source.sortOrder < targetIndex) {
    targetIndex -= 1;
  }

  return targetIndex;
}

function arePointerDropTargetsEqual(
  first: PointerDropTarget | null,
  second: PointerDropTarget | null,
): boolean {
  if (first === second) {
    return true;
  }
  if (!first || !second) {
    return false;
  }

  return (
    first.index === second.index &&
    first.intent === second.intent &&
    first.targetNode.id === second.targetNode.id
  );
}

function formatTime(minutes?: number | null) {
  if (!minutes) return 'No estimate yet';
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  const remainder = minutes % 60;
  return remainder ? `${hours}h ${remainder}m` : `${hours}h`;
}

function formatStatus(status: StrategyNode['status']) {
  if (status === 'in-progress') return 'In progress';
  if (status === 'done') return 'Done';
  return 'To do';
}

function createAttachmentId(file: File) {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) {
    return crypto.randomUUID();
  }

  return `${Date.now()}-${Math.random().toString(36).slice(2)}-${file.name}`;
}

function readImageAttachment(file: File): Promise<NodeImageAttachment> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();

    reader.onload = () => {
      const dataUrl = typeof reader.result === 'string' ? reader.result : '';
      if (!dataUrl.startsWith('data:image/')) {
        reject(new Error('Only image files are supported.'));
        return;
      }

      resolve({
        id: createAttachmentId(file),
        name: file.name,
        dataUrl,
      });
    };

    reader.onerror = () => {
      reject(reader.error ?? new Error(`Failed to read ${file.name}.`));
    };

    reader.readAsDataURL(file);
  });
}

export function PlanningWorkspacePanel({
  activeDestination,
  activeCategory,
  categoryById,
  isNodesLoading,
  activeNodes,
  backlogNodes,
  archivedNodes,
  stagedNodeIds,
  scheduledNodeIdSet,
  isBacklogExpanded,
  onBacklogToggle,
  isArchivedExpanded,
  onArchivedToggle,
  onAddNode,
  onAddPlannedTask,
  planningDay,
  onPlanningDayChange,
  isCalendarOpen,
  onCalendarToggle,
  onStatusChange,
  onTitleChange,
  onStageNode,
  onToggleStageNode,
  onSchedulePointerDrop,
  pendingFocusNodeId,
  onAutoFocusHandled,
  calendarFocusRequest,
  onCalendarFocusHandled,
  onAiContextTaskChange,
}: PlanningWorkspacePanelProps) {
  const isAllWorkspace = activeDestination === 'all';
  const isWorkspaceView =
    isAllWorkspace || (activeDestination === 'category' && activeCategory !== null);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const isCompact = useMediaQuery('(max-width: 1023px)');
  const [desktopRepositoryOpen, setDesktopRepositoryOpen] = useState(true);
  const [mobileRepositoryOpen, setMobileRepositoryOpen] = useState(false);
  const repositoryOpen = isCompact ? mobileRepositoryOpen : desktopRepositoryOpen;
  const setRepositoryOpen = isCompact ? setMobileRepositoryOpen : setDesktopRepositoryOpen;
  const [openBriefNodeId, setOpenBriefNodeId] = useState<string | null>(null);
  const [expandedNodeIds, setExpandedNodeIds] = useState<Record<string, boolean>>({});
  const [hoveredTreeNodeId, setHoveredTreeNodeId] = useState<string | null>(null);
  const [treeViewport, setTreeViewport] = useState({
    scrollTop: 0,
    height: 0,
    scrollbarWidth: 0,
  });
  const [pointerDragState, setPointerDragState] = useState<PointerTreeDragState | null>(null);
  const [pointerDropTarget, setPointerDropTarget] = useState<PointerDropTarget | null>(null);
  const [briefNotes, setBriefNotes] = useState<Record<string, string>>({});
  const [titleDrafts, setTitleDrafts] = useState<Record<string, string>>({});
  const [tagDrafts, setTagDrafts] = useState<Record<string, string>>({});
  const [isUploadingImages, setIsUploadingImages] = useState(false);
  const [calendarSpotlightNodeId, setCalendarSpotlightNodeId] = useState<string | null>(null);
  const [treeSearchQuery, setTreeSearchQuery] = useState('');
  const [treeViewSettingsByWorkspace, setTreeViewSettingsByWorkspace] =
    useState<TreeViewSettingsByWorkspace>(loadTreeViewSettings);
  const [openTreeMenu, setOpenTreeMenu] = useState<'filter' | 'sort' | null>(null);
  const [isRecurrenceDialogOpen, setIsRecurrenceDialogOpen] = useState(false);
  const [isWorkspaceMenuOpen, setIsWorkspaceMenuOpen] = useState(false);
  const [workspaceDialog, setWorkspaceDialog] = useState<{
    mode: WorkspaceDialogMode;
    target: Category;
  } | null>(null);
  const [workspaceNameDraft, setWorkspaceNameDraft] = useState('');
  const [deleteStep, setDeleteStep] = useState<1 | 2>(1);
  const [deleteConfirmation, setDeleteConfirmation] = useState('');
  const [isWorkspaceActionPending, setIsWorkspaceActionPending] = useState(false);
  const updateCategory = useCategoryStore((state) => state.updateCategory);
  const deleteCategory = useCategoryStore((state) => state.deleteCategory);
  const updateNode = useNodeStore((state) => state.updateNode);
  const moveNode = useNodeStore((state) => state.moveNode);
  const imageInputRef = useRef<HTMLInputElement>(null);
  const workspaceMenuRef = useRef<HTMLDivElement>(null);
  const treeScrollRef = useRef<HTMLDivElement>(null);
  const suppressClickUntilRef = useRef(0);
  const pointerDragPreviewRef = useRef<HTMLDivElement>(null);
  const viewportFrameRef = useRef<number | null>(null);
  const pointerDragFrameRef = useRef<number | null>(null);
  const pointerDropTargetRef = useRef<PointerDropTarget | null>(null);
  const calendarFocusFrameRef = useRef<number | null>(null);
  const calendarSpotlightTimerRef = useRef<number | null>(null);
  const treeViewSettingsKey = isAllWorkspace
    ? 'all'
    : activeCategory
      ? `category:${activeCategory.id}`
      : 'none';
  const treeViewSettings =
    treeViewSettingsByWorkspace[treeViewSettingsKey] ?? DEFAULT_TREE_VIEW_SETTINGS;
  const treeFilters = treeViewSettings.filters;
  const treeSort = treeViewSettings.sort;
  const activeTreeFilterCount = Object.values(treeFilters).filter((value) => value !== 'both').length;
  const hasActiveTreeFilters = activeTreeFilterCount > 0;
  const treeFilterSummary = [
    `status ${filterValueLabel('status', treeFilters.status)}`,
    `schedule ${filterValueLabel('schedule', treeFilters.schedule)}`,
    `recurrence ${filterValueLabel('recurrence', treeFilters.recurrence)}`,
  ].join('; ');

  const updateTreeViewSettings = useCallback(
    (updates: Partial<TreeViewSettings>) => {
      setTreeViewSettingsByWorkspace((current) => ({
        ...current,
        [treeViewSettingsKey]: {
          ...(current[treeViewSettingsKey] ?? DEFAULT_TREE_VIEW_SETTINGS),
          ...updates,
        },
      }));
    },
    [treeViewSettingsKey],
  );

  const updateTreeFilter = useCallback(
    (facet: TreeFilterFacet, value: TreeFilterValue) => {
      setTreeViewSettingsByWorkspace((current) => {
        const currentSettings = current[treeViewSettingsKey] ?? DEFAULT_TREE_VIEW_SETTINGS;
        return {
          ...current,
          [treeViewSettingsKey]: {
            ...currentSettings,
            filters: {
              ...currentSettings.filters,
              [facet]: value,
            },
          },
        };
      });
    },
    [treeViewSettingsKey],
  );

  const toggleTreeFilterOption = useCallback(
    (facet: TreeFilterFacet, polarity: TreeFilterPolarity) => {
      const currentValue = treeFilters[facet];
      const positive =
        polarity === 'positive'
          ? !isFilterPolaritySelected(currentValue, 'positive')
          : isFilterPolaritySelected(currentValue, 'positive');
      const negative =
        polarity === 'negative'
          ? !isFilterPolaritySelected(currentValue, 'negative')
          : isFilterPolaritySelected(currentValue, 'negative');
      updateTreeFilter(facet, filterValueFromSelections(positive, negative));
    },
    [treeFilters, updateTreeFilter],
  );

  const resetTreeFilters = useCallback(() => {
    updateTreeViewSettings({ filters: DEFAULT_TREE_FILTERS });
  }, [updateTreeViewSettings]);

  useEffect(() => {
    persistTreeViewSettings(treeViewSettingsByWorkspace);
  }, [treeViewSettingsByWorkspace]);

  useEffect(() => {
    if (!isWorkspaceMenuOpen) {
      return undefined;
    }

    const closeMenu = (event: PointerEvent) => {
      if (workspaceMenuRef.current?.contains(event.target as Node)) {
        return;
      }
      setIsWorkspaceMenuOpen(false);
    };
    const closeMenuWithEscape = (event: globalThis.KeyboardEvent) => {
      if (event.key === 'Escape') {
        setIsWorkspaceMenuOpen(false);
      }
    };

    window.addEventListener('pointerdown', closeMenu);
    window.addEventListener('keydown', closeMenuWithEscape);
    return () => {
      window.removeEventListener('pointerdown', closeMenu);
      window.removeEventListener('keydown', closeMenuWithEscape);
    };
  }, [isWorkspaceMenuOpen]);

  useEffect(() => {
    if (!workspaceDialog) {
      return undefined;
    }

    const closeDialogWithEscape = (event: globalThis.KeyboardEvent) => {
      if (event.key === 'Escape' && !isWorkspaceActionPending) {
        setWorkspaceDialog(null);
      }
    };

    window.addEventListener('keydown', closeDialogWithEscape);
    return () => window.removeEventListener('keydown', closeDialogWithEscape);
  }, [isWorkspaceActionPending, workspaceDialog]);

  const visibleTreeNodes = useMemo(
    () => [...flattenNodes(activeNodes), ...flattenNodes(backlogNodes), ...flattenNodes(archivedNodes)],
    [activeNodes, archivedNodes, backlogNodes],
  );
  const stagedNodeIdSet = useMemo(() => new Set(stagedNodeIds), [stagedNodeIds]);
  const preparedActiveNodes = useMemo(
    () => prepareTreeNodes(activeNodes, treeSearchQuery, treeFilters, treeSort, scheduledNodeIdSet),
    [activeNodes, scheduledNodeIdSet, treeFilters, treeSearchQuery, treeSort],
  );
  const preparedBacklogNodes = useMemo(
    () => prepareTreeNodes(backlogNodes, treeSearchQuery, treeFilters, treeSort, scheduledNodeIdSet),
    [backlogNodes, scheduledNodeIdSet, treeFilters, treeSearchQuery, treeSort],
  );
  const preparedArchivedNodes = useMemo(
    () => prepareTreeNodes(archivedNodes, treeSearchQuery, treeFilters, treeSort, scheduledNodeIdSet),
    [archivedNodes, scheduledNodeIdSet, treeFilters, treeSearchQuery, treeSort],
  );
  const visibleNodeById = useMemo(
    () => new Map(visibleTreeNodes.map((node) => [node.id, node])),
    [visibleTreeNodes],
  );

  const effectiveSelectedNodeId =
    selectedNodeId && visibleTreeNodes.some((node) => node.id === selectedNodeId)
      ? selectedNodeId
      : null;
  const effectiveOpenBriefNodeId =
    openBriefNodeId && visibleTreeNodes.some((node) => node.id === openBriefNodeId)
      ? openBriefNodeId
      : null;
  const handleOpenBrief = useCallback((id: string) => {
    setSelectedNodeId(id);
    setOpenBriefNodeId(id);
  }, []);
  const focusNode =
    visibleTreeNodes.find((node) => node.id === effectiveOpenBriefNodeId) ?? null;
  const currentStrategistNode =
    focusNode ??
    visibleTreeNodes.find((node) => node.id === effectiveSelectedNodeId) ??
    null;
  useEffect(() => {
    onAiContextTaskChange(currentStrategistNode);
  }, [currentStrategistNode, onAiContextTaskChange]);
  const focusChecklist = focusNode?.children.slice(0, 4) ?? [];
  const backlogTreeCount = countVisibleTreeNodes(preparedBacklogNodes, 'backlog');
  const archivedTreeCount = countVisibleTreeNodes(preparedArchivedNodes, 'archived');
  const forceExpandedTree = treeSearchQuery.trim().length > 0 || hasActiveTreeFilters;
  const virtualTreeRows = useMemo<VirtualTreeRow[]>(() => {
    const rows: VirtualTreeRow[] = [];

    rows.push(
      ...(isAllWorkspace
        ? collectWorkspaceGroupedRows(
            preparedActiveNodes,
            'active',
            expandedNodeIds,
            effectiveSelectedNodeId,
            forceExpandedTree,
            categoryById,
          )
        : collectVirtualNodeRows(
            preparedActiveNodes,
            'active',
            expandedNodeIds,
            effectiveSelectedNodeId,
            0,
            [],
            forceExpandedTree,
          )),
    );

    if (!isNodesLoading && preparedActiveNodes.length === 0) {
      rows.push({ kind: 'empty', key: 'empty-active' });
    }

    if (preparedBacklogNodes.length > 0) {
      rows.push({
        kind: 'section',
        key: 'section-backlog',
        section: 'backlog',
        label: 'Backlog',
        count: backlogTreeCount,
        isExpanded: isBacklogExpanded,
      });

      if (isBacklogExpanded) {
        rows.push(
          ...(isAllWorkspace
            ? collectWorkspaceGroupedRows(
                preparedBacklogNodes,
                'backlog',
                expandedNodeIds,
                effectiveSelectedNodeId,
                forceExpandedTree,
                categoryById,
              )
            : collectVirtualNodeRows(
                preparedBacklogNodes,
                'backlog',
                expandedNodeIds,
                effectiveSelectedNodeId,
                0,
                [],
                forceExpandedTree,
              )),
        );
      }
    }

    if (preparedArchivedNodes.length > 0) {
      rows.push({
        kind: 'section',
        key: 'section-archived',
        section: 'archived',
        label: 'Archived',
        count: archivedTreeCount,
        isExpanded: isArchivedExpanded,
      });

      if (isArchivedExpanded) {
        rows.push(
          ...(isAllWorkspace
            ? collectWorkspaceGroupedRows(
                preparedArchivedNodes,
                'archived',
                expandedNodeIds,
                effectiveSelectedNodeId,
                forceExpandedTree,
                categoryById,
              )
            : collectVirtualNodeRows(
                preparedArchivedNodes,
                'archived',
                expandedNodeIds,
                effectiveSelectedNodeId,
                0,
                [],
                forceExpandedTree,
              )),
        );
      }
    }

    return rows;
  }, [
    categoryById,
    forceExpandedTree,
    isAllWorkspace,
    preparedActiveNodes,
    preparedArchivedNodes,
    archivedTreeCount,
    preparedBacklogNodes,
    backlogTreeCount,
    effectiveSelectedNodeId,
    expandedNodeIds,
    isArchivedExpanded,
    isBacklogExpanded,
    isNodesLoading,
  ]);
  const rowLayout = useMemo(() => {
    let top = 0;
    return virtualTreeRows.map(row => {
      const height = row.kind === 'node' ? TREE_ROW_HEIGHT : row.kind === 'empty' ? 88 : 40;
      const layout = { top, height };
      top += height;
      return layout;
    });
  }, [virtualTreeRows]);
  const totalTreeHeight = rowLayout.length ? rowLayout.at(-1)!.top + rowLayout.at(-1)!.height : 0;
  const visibleVirtualRows = virtualTreeRows.map((row, index) => ({ row, index }))
    .filter(({ index }) => rowLayout[index].top + rowLayout[index].height >= treeViewport.scrollTop - TREE_OVERSCAN_ROWS * TREE_ROW_HEIGHT
      && rowLayout[index].top <= treeViewport.scrollTop + treeViewport.height + TREE_OVERSCAN_ROWS * TREE_ROW_HEIGHT);
  const hoveredTreeNode = hoveredTreeNodeId ? visibleNodeById.get(hoveredTreeNodeId) ?? null : null;
  const currentTitleDraft = focusNode ? titleDrafts[focusNode.id] ?? focusNode.title : '';
  const currentBriefNotes = focusNode ? briefNotes[focusNode.id] ?? focusNode.notes ?? '' : '';
  const currentTagDraft = focusNode ? tagDrafts[focusNode.id] ?? '' : '';
  const updateTreeViewport = useCallback(() => {
    const scrollElement = treeScrollRef.current;
    if (!scrollElement) {
      return;
    }

    const nextViewport = {
      scrollTop: scrollElement.scrollTop,
      height: scrollElement.clientHeight,
      scrollbarWidth: scrollElement.offsetWidth - scrollElement.clientWidth,
    };
    setTreeViewport((current) =>
      current.scrollTop === nextViewport.scrollTop &&
      current.height === nextViewport.height &&
      current.scrollbarWidth === nextViewport.scrollbarWidth
        ? current
        : nextViewport,
    );
  }, []);

  const handleTreeScroll = useCallback(() => {
    if (viewportFrameRef.current !== null) {
      return;
    }

    viewportFrameRef.current = window.requestAnimationFrame(() => {
      viewportFrameRef.current = null;
      updateTreeViewport();
    });
  }, [updateTreeViewport]);

  const handleToggleExpanded = useCallback((id: string) => {
    setExpandedNodeIds((current) => ({
      ...current,
      [id]: !(current[id] ?? true),
    }));
  }, []);

  useEffect(() => {
    if (!calendarFocusRequest || !isWorkspaceView || isNodesLoading) {
      return;
    }

    const targetNode = visibleNodeById.get(calendarFocusRequest.nodeId);
    if (!targetNode) {
      return;
    }

    const rowIndex = virtualTreeRows.findIndex(
      (row) => row.kind === 'node' && row.node.id === targetNode.id,
    );

    calendarFocusFrameRef.current = window.requestAnimationFrame(() => {
      calendarFocusFrameRef.current = null;
      setSelectedNodeId(targetNode.id);
      setOpenBriefNodeId(null);

      if (targetNode.isBacklog && !isBacklogExpanded) {
        onBacklogToggle();
        return;
      }

      if (isArchivedNode(targetNode) && !isArchivedExpanded) {
        onArchivedToggle();
        return;
      }

      const scrollElement = treeScrollRef.current;
      if (rowIndex < 0 || !scrollElement) {
        return;
      }

      const centeredScrollTop = Math.max(
        0,
        rowLayout[rowIndex].top - Math.max(0, scrollElement.clientHeight / 2 - TREE_ROW_HEIGHT),
      );
      scrollElement.scrollTo({ top: centeredScrollTop, behavior: 'smooth' });
      updateTreeViewport();

      setCalendarSpotlightNodeId(targetNode.id);
      if (calendarSpotlightTimerRef.current !== null) {
        window.clearTimeout(calendarSpotlightTimerRef.current);
      }
      calendarSpotlightTimerRef.current = window.setTimeout(() => {
        setCalendarSpotlightNodeId((current) =>
          current === targetNode.id ? null : current,
        );
        calendarSpotlightTimerRef.current = null;
      }, 2200);

      onCalendarFocusHandled(calendarFocusRequest.requestId);
    });

    return () => {
      if (calendarFocusFrameRef.current !== null) {
        window.cancelAnimationFrame(calendarFocusFrameRef.current);
        calendarFocusFrameRef.current = null;
      }
    };
  }, [
    calendarFocusRequest,
    isArchivedExpanded,
    isBacklogExpanded,
    isWorkspaceView,
    isNodesLoading,
    onArchivedToggle,
    onBacklogToggle,
    onCalendarFocusHandled,
    updateTreeViewport,
    virtualTreeRows,
    rowLayout,
    visibleNodeById,
  ]);

  const resolvePointerDropTarget = useCallback(
    (
      clientX: number,
      clientY: number,
      sourceNode: StrategyNode,
    ): PointerDropTarget | null => {
      const scrollElement = treeScrollRef.current;
      if (!scrollElement || virtualTreeRows.length === 0) {
        return null;
      }

      const rect = scrollElement.getBoundingClientRect();
      if (
        clientX < rect.left ||
        clientX > rect.right ||
        clientY < rect.top ||
        clientY > rect.bottom
      ) {
        return null;
      }

      const rootDrop = scrollElement.querySelector<HTMLElement>('[data-tree-root-drop]')?.getBoundingClientRect();
      if (rootDrop && clientY >= rootDrop.top && clientY <= rootDrop.bottom) {
        return { index: -1, targetNode: sourceNode, intent: 'after', root: true };
      }
      const absoluteY = scrollElement.scrollTop + clientY - rect.top;
      const rowIndex = rowLayout.findIndex(layout => absoluteY >= layout.top && absoluteY < layout.top + layout.height);
      if (rowIndex < 0) return null;
      const row = virtualTreeRows[rowIndex];
      if (row.kind !== 'node') {
        return null;
      }

      if (row.node.categoryId !== sourceNode.categoryId || row.node.id === sourceNode.id || containsNodeId(sourceNode, row.node.id)) {
        return null;
      }

      const offsetWithinRow = absoluteY - rowLayout[rowIndex].top;
      const topZone = TREE_ROW_HEIGHT * 0.28;
      const bottomZone = TREE_ROW_HEIGHT * 0.72;
      const intent =
        offsetWithinRow < topZone
          ? 'before'
          : offsetWithinRow > bottomZone
            ? 'after'
            : 'inside';

      return {
        index: rowIndex,
        targetNode: row.node,
        intent,
      };
    },
    [rowLayout, virtualTreeRows],
  );

  const isPointerInsideSelectedPanel = useCallback((clientX: number, clientY: number) => {
    const selectedSurface = document.querySelector<HTMLElement>('.selected-list-surface');
    if (!selectedSurface) {
      return false;
    }

    const rect = selectedSurface.getBoundingClientRect();
    return clientX >= rect.left && clientX <= rect.right && clientY >= rect.top && clientY <= rect.bottom;
  }, []);

  const applyPointerDrop = useCallback(
    (source: NodeDragPayload, target: PointerDropTarget) => {
      const parentId =
        target.root ? null : target.intent === 'inside' ? target.targetNode.id : (target.targetNode.parentId ?? null);
      const targetIndex =
        target.root ? useNodeStore.getState().nodes.filter(node => node.id !== source.id && node.categoryId === source.categoryId && !node.parentId && node.isBacklog === target.targetNode.isBacklog).length : target.intent === 'inside'
          ? target.targetNode.children.length
          : getPointerReorderIndex(target.intent, target.targetNode, source);

      if (target.intent === 'inside') setExpandedNodeIds(current => ({ ...current, [target.targetNode.id]: true }));
      updateTreeViewSettings({ sort: 'manual' });
      void moveNode(source.id, {
        parentId,
        targetIndex,
        categoryId: target.targetNode.categoryId,
        isBacklog: target.targetNode.isBacklog,
      }).catch(() => undefined);
    },
    [moveNode, updateTreeViewSettings],
  );

  const handlePointerDragStart = useCallback(
    (node: StrategyNode, event: ReactPointerEvent<HTMLDivElement>) => {
      if (event.button !== 0) {
        return;
      }

      if (
        event.target instanceof Element &&
        event.target.closest('button, input, textarea, select, [contenteditable="true"]')
      ) {
        return;
      }

      event.preventDefault();
      const source: NodeDragPayload = {
        id: node.id,
        parentId: node.parentId ?? null,
        categoryId: node.categoryId,
        sortOrder: node.sortOrder ?? 0,
      };
      const startX = event.clientX;
      const startY = event.clientY;
      let didStartDrag = false;
      let didCommitFirstDragFrame = false;
      let latestPreviewX = startX;
      let latestPreviewY = startY;

      const updatePreviewPosition = () => {
        pointerDragFrameRef.current = null;
        pointerDragPreviewRef.current?.style.setProperty(
          'transform',
          `translate3d(${latestPreviewX + 14}px, ${latestPreviewY + 12}px, 0)`,
        );
      };

      const schedulePreviewPosition = (x: number, y: number) => {
        latestPreviewX = x;
        latestPreviewY = y;
        if (pointerDragFrameRef.current !== null) {
          return;
        }

        pointerDragFrameRef.current = window.requestAnimationFrame(updatePreviewPosition);
      };

      const updatePointerDropTarget = (nextDropTarget: PointerDropTarget | null) => {
        if (arePointerDropTargetsEqual(pointerDropTargetRef.current, nextDropTarget)) {
          return;
        }

        pointerDropTargetRef.current = nextDropTarget;
        setPointerDropTarget(nextDropTarget);
      };

      const clearPointerDrag = () => {
        if (pointerDragFrameRef.current !== null) {
          window.cancelAnimationFrame(pointerDragFrameRef.current);
          pointerDragFrameRef.current = null;
        }
        pointerDropTargetRef.current = null;
        document.body.classList.remove('app-pointer-dragging');
        useNodeStore.getState().setActiveDragPayload(null);
        setPointerDragState(null);
        setPointerDropTarget(null);
        setHoveredTreeNodeId(null);
        window.getSelection()?.removeAllRanges();
      };

      const scrollNearEdges = (clientY: number) => {
        const scrollElement = treeScrollRef.current;
        if (!scrollElement) {
          return;
        }

        const rect = scrollElement.getBoundingClientRect();
        if (clientY < rect.top + TREE_AUTOSCROLL_EDGE) {
          scrollElement.scrollTop -= TREE_AUTOSCROLL_STEP;
          updateTreeViewport();
          return;
        }
        if (clientY > rect.bottom - TREE_AUTOSCROLL_EDGE) {
          scrollElement.scrollTop += TREE_AUTOSCROLL_STEP;
          updateTreeViewport();
        }
      };

      const handlePointerMove = (moveEvent: PointerEvent) => {
        const deltaX = moveEvent.clientX - startX;
        const deltaY = moveEvent.clientY - startY;
        const distance = Math.hypot(deltaX, deltaY);

        if (!didStartDrag && distance < POINTER_DRAG_THRESHOLD) {
          return;
        }

        moveEvent.preventDefault();
        if (!didStartDrag) {
          didStartDrag = true;
          document.body.classList.add('app-pointer-dragging');
          useNodeStore.getState().setActiveDragPayload(source);
          window.getSelection()?.removeAllRanges();
          setHoveredTreeNodeId(null);
        }

        scrollNearEdges(moveEvent.clientY);
        const nextDropTarget = resolvePointerDropTarget(
          moveEvent.clientX,
          moveEvent.clientY,
          node,
        );
        const nextDragState = {
          source,
          node,
          x: moveEvent.clientX,
          y: moveEvent.clientY,
        };

        if (!didCommitFirstDragFrame) {
          didCommitFirstDragFrame = true;
          setPointerDragState(nextDragState);
          updatePointerDropTarget(nextDropTarget);
          schedulePreviewPosition(moveEvent.clientX, moveEvent.clientY);
          return;
        }

        updatePointerDropTarget(nextDropTarget);
        schedulePreviewPosition(moveEvent.clientX, moveEvent.clientY);
      };

      const handlePointerUp = (upEvent: PointerEvent) => {
        window.removeEventListener('pointermove', handlePointerMove);
        window.removeEventListener('pointerup', handlePointerUp);
        window.removeEventListener('pointercancel', handlePointerCancel);

        if (!didStartDrag) {
          return;
        }

        upEvent.preventDefault();
        suppressClickUntilRef.current = Date.now() + 400;
        const finalDropTarget =
          resolvePointerDropTarget(upEvent.clientX, upEvent.clientY, node);
        clearPointerDrag();

        const weekDay = document.elementFromPoint(upEvent.clientX, upEvent.clientY)?.closest<HTMLElement>('[data-week-day]')?.dataset.weekDay;
        if (weekDay) {
          void updateNode(source.id, { plannedDate: weekDay, ...(node.isBacklog ? { isBacklog: false } : {}) }).catch(() => undefined);
          return;
        }
        if (onSchedulePointerDrop?.(source, upEvent.clientX, upEvent.clientY)) {
          return;
        }

        if (isPointerInsideSelectedPanel(upEvent.clientX, upEvent.clientY)) {
          onStageNode(source.id);
          return;
        }

        if (finalDropTarget) {
          window.requestAnimationFrame(() => {
            applyPointerDrop(source, finalDropTarget);
          });
        }
      };

      const handlePointerCancel = () => {
        window.removeEventListener('pointermove', handlePointerMove);
        window.removeEventListener('pointerup', handlePointerUp);
        window.removeEventListener('pointercancel', handlePointerCancel);
        clearPointerDrag();
      };

      window.addEventListener('pointermove', handlePointerMove, { passive: false });
      window.addEventListener('pointerup', handlePointerUp);
      window.addEventListener('pointercancel', handlePointerCancel);
    },
    [
      applyPointerDrop,
      isPointerInsideSelectedPanel,
      onSchedulePointerDrop,
      onStageNode,
      resolvePointerDropTarget,
      updateNode,
      updateTreeViewport,
    ],
  );

  useEffect(() => {
    updateTreeViewport();
    const scrollElement = treeScrollRef.current;
    if (!scrollElement || typeof ResizeObserver === 'undefined') {
      return;
    }

    const observer = new ResizeObserver(updateTreeViewport);
    observer.observe(scrollElement);
    return () => {
      observer.disconnect();
    };
  }, [updateTreeViewport, virtualTreeRows.length]);

  useEffect(() => {
    return () => {
      if (viewportFrameRef.current !== null) {
        window.cancelAnimationFrame(viewportFrameRef.current);
      }
      if (pointerDragFrameRef.current !== null) {
        window.cancelAnimationFrame(pointerDragFrameRef.current);
      }
      if (calendarFocusFrameRef.current !== null) {
        window.cancelAnimationFrame(calendarFocusFrameRef.current);
      }
      if (calendarSpotlightTimerRef.current !== null) {
        window.clearTimeout(calendarSpotlightTimerRef.current);
      }
      pointerDropTargetRef.current = null;
      if (document.body.classList.contains('app-pointer-dragging')) useNodeStore.getState().setActiveDragPayload(null);
      document.body.classList.remove('app-pointer-dragging');
    };
  }, []);

  const commitTags = async (nextTags: string[]) => {
    if (!focusNode) {
      return;
    }

    const normalizedTags = Array.from(
      new Set(nextTags.map((tag) => tag.trim()).filter(Boolean)),
    );
    await updateNode(focusNode.id, { tags: normalizedTags });
  };

  const commitTitle = () => {
    if (!focusNode) {
      return;
    }

    const nextTitle = currentTitleDraft.trim() || focusNode.title;
    setTitleDrafts((current) => ({
      ...current,
      [focusNode.id]: nextTitle,
    }));

    if (nextTitle !== focusNode.title) {
      onTitleChange(focusNode.id, nextTitle);
    }
  };

  const handleTitleKeyDown = (event: ReactKeyboardEvent<HTMLTextAreaElement>) => {
    if (!focusNode) {
      return;
    }

    if (event.key === 'Enter') {
      event.preventDefault();
      commitTitle();
      event.currentTarget.blur();
      return;
    }

    if (event.key === 'Escape') {
      event.preventDefault();
      setTitleDrafts((current) => ({
        ...current,
        [focusNode.id]: focusNode.title,
      }));
      event.currentTarget.blur();
    }
  };

  const handleAddTag = async () => {
    if (!focusNode) {
      return;
    }

    const nextTag = currentTagDraft.trim();
    if (!nextTag) {
      return;
    }

    await commitTags([...focusNode.tags, nextTag]);
    setTagDrafts((current) => ({
      ...current,
      [focusNode.id]: '',
    }));
  };

  const handleTagKeyDown = async (event: ReactKeyboardEvent<HTMLInputElement>) => {
    if (event.key !== 'Enter') {
      return;
    }

    event.preventDefault();
    await handleAddTag();
  };

  const handleRemoveTag = async (tagToRemove: string) => {
    if (!focusNode) {
      return;
    }

    await commitTags(focusNode.tags.filter((tag) => tag !== tagToRemove));
  };

  const handleImageInputChange = async (event: ChangeEvent<HTMLInputElement>) => {
    if (!focusNode) {
      event.currentTarget.value = '';
      return;
    }

    const files = Array.from(event.currentTarget.files ?? []).filter((file) =>
      file.type.startsWith('image/'),
    );
    event.currentTarget.value = '';

    if (files.length === 0) {
      return;
    }

    setIsUploadingImages(true);
    try {
      const nextAttachments = await Promise.all(files.map(readImageAttachment));
      await updateNode(focusNode.id, {
        imageAttachments: [...focusNode.imageAttachments, ...nextAttachments],
      });
    } finally {
      setIsUploadingImages(false);
    }
  };

  const handleRemoveAttachment = async (attachmentId: string) => {
    if (!focusNode) {
      return;
    }

    await updateNode(focusNode.id, {
      imageAttachments: focusNode.imageAttachments.filter(
        (attachment) => attachment.id !== attachmentId,
      ),
    });
  };

  const openWorkspaceDialog = (mode: WorkspaceDialogMode) => {
    if (!activeCategory || isAllWorkspace) {
      return;
    }

    setIsWorkspaceMenuOpen(false);
    setWorkspaceDialog({ mode, target: activeCategory });
    setWorkspaceNameDraft(activeCategory.name);
    setDeleteStep(1);
    setDeleteConfirmation('');
  };

  const closeWorkspaceDialog = () => {
    if (isWorkspaceActionPending) {
      return;
    }

    setWorkspaceDialog(null);
    setWorkspaceNameDraft('');
    setDeleteStep(1);
    setDeleteConfirmation('');
  };

  const renameWorkspace = async () => {
    if (!workspaceDialog || workspaceDialog.mode !== 'rename' || isWorkspaceActionPending) {
      return;
    }

    const trimmedName = workspaceNameDraft.trim();
    if (!trimmedName) {
      return;
    }

    setIsWorkspaceActionPending(true);
    const wasUpdated = await updateCategory(workspaceDialog.target.id, trimmedName);
    setIsWorkspaceActionPending(false);
    if (wasUpdated) {
      setWorkspaceDialog(null);
      setWorkspaceNameDraft('');
    }
  };

  const deleteWorkspace = async () => {
    if (
      !workspaceDialog ||
      workspaceDialog.mode !== 'delete' ||
      deleteConfirmation !== workspaceDialog.target.name ||
      isWorkspaceActionPending
    ) {
      return;
    }

    setIsWorkspaceActionPending(true);
    const wasDeleted = await deleteCategory(workspaceDialog.target.id);
    setIsWorkspaceActionPending(false);
    if (wasDeleted) {
      setWorkspaceDialog(null);
      setDeleteStep(1);
      setDeleteConfirmation('');
    }
  };

  if (activeDestination === 'category' && !activeCategory) {
    return (
      <section className="planning-panel" aria-labelledby="planning-panel-heading">
        <header className="planning-panel-header">
          <div>
            <h2 className="planning-panel-title" id="planning-panel-heading">
              Category workspace
            </h2>
          </div>
        </header>

        <div className="planning-panel-placeholder">
          <div className="planning-empty-state">
            <p className="planning-empty-state-title">No workspaces yet</p>
            <p className="planning-empty-state-body">
              Add a workspace from the sidebar to start building the planning tree.
            </p>
          </div>
        </div>
      </section>
    );
  }

    if (!isWorkspaceView) {
    if (activeDestination === 'settings') {
      return (
        <section className="planning-panel" aria-labelledby="planning-panel-heading">
          <header className="planning-panel-header">
            <div>
              <p className="planning-panel-eyebrow">System controls</p>
              <h2 className="planning-panel-title" id="planning-panel-heading">
                Settings
              </h2>
            </div>
          </header>

          <div className="planning-panel-placeholder">
            <Suspense fallback={<p aria-live="polite">Loading settings…</p>}>
              <ChatGptAuthSettings />
            </Suspense>
          </div>
        </section>
      );
    }

    const destination =
      utilityContent[
        activeDestination as Exclude<AppDestination, 'all' | 'category'>
      ];

    return (
      <section className="planning-panel" aria-labelledby="planning-panel-heading">
        <header className="planning-panel-header">
          <div>
            <p className="planning-panel-eyebrow">{destination.eyebrow}</p>
            <h2 className="planning-panel-title" id="planning-panel-heading">
              {destination.title}
            </h2>
          </div>
        </header>

        <div className="planning-panel-placeholder">
          <article className="planning-surface planning-utility-surface">
            <p className="planning-utility-copy">{destination.body}</p>
            <p className="planning-utility-detail">{destination.detail}</p>

            {activeCategory && (
              <div className="planning-context-chip">
                <span
                  className="planning-context-dot"
                  style={{ backgroundColor: activeCategory.accentColor }}
                  aria-hidden="true"
                />
                Last category workspace: {activeCategory.name}
              </div>
            )}
          </article>
        </div>
      </section>
    );
  }

  const workspaceName = isAllWorkspace ? 'All' : activeCategory?.name ?? 'Workspace';
  const workspaceAccentColor = isAllWorkspace
    ? '#6366f1'
    : activeCategory?.accentColor ?? '#6366f1';

  return (
    <section className="planning-panel" aria-labelledby="planning-panel-heading">
      <header className="planning-panel-header">
        <div className="planning-panel-title-group">
          <span
            className="planning-panel-workspace-icon"
            style={{ '--cat-color': workspaceAccentColor } as CSSProperties}
          >
            {isAllWorkspace ? (
              <Layers3 size={17} strokeWidth={1.8} aria-hidden="true" />
            ) : (
              <WorkspaceIcon slug={activeCategory?.iconSlug ?? 'circle'} size={17} />
            )}
          </span>
          <h2 className="planning-panel-title" id="planning-panel-heading">
            {workspaceName} week
          </h2>
        </div>
        <div className="planning-view-switch" role="group" aria-label="Floating workspace panels">
          <button type="button" aria-expanded={repositoryOpen} aria-controls="floating-task-repository" onClick={() => setRepositoryOpen(value => !value)}><ListTree size={14} aria-hidden="true" />Tree</button>
          <button type="button" aria-label={isCalendarOpen ? 'Hide calendar panel' : 'Show calendar panel'} aria-expanded={isCalendarOpen} onClick={onCalendarToggle}><CalendarDays size={14} aria-hidden="true" />Calendar</button>
        </div>
        {!isAllWorkspace && activeCategory ? (
          <div className="planning-panel-workspace-menu-anchor" ref={workspaceMenuRef}>
            <button
              type="button"
              className="planning-panel-more-btn"
              aria-label={`Workspace options for ${activeCategory.name}`}
              aria-haspopup="menu"
              aria-expanded={isWorkspaceMenuOpen}
              aria-controls={`planning-workspace-menu-${activeCategory.id}`}
              title={`${activeCategory.name} workspace options`}
              onClick={() => setIsWorkspaceMenuOpen((isOpen) => !isOpen)}
            >
              <MoreHorizontal size={18} aria-hidden="true" />
            </button>
            {isWorkspaceMenuOpen ? (
              <div
                id={`planning-workspace-menu-${activeCategory.id}`}
                className="planning-panel-workspace-menu"
                role="menu"
                aria-label={`${activeCategory.name} workspace options`}
              >
                <button
                  type="button"
                  role="menuitem"
                  onClick={() => openWorkspaceDialog('rename')}
                >
                  <Pencil size={15} aria-hidden="true" />
                  Rename workspace
                </button>
                <button
                  type="button"
                  role="menuitem"
                  className="danger"
                  onClick={() => openWorkspaceDialog('delete')}
                >
                  <Trash2 size={15} aria-hidden="true" />
                  Delete workspace
                </button>
              </div>
            ) : null}
          </div>
        ) : null}
      </header>

      <div className={`planning-panel-body no-brief is-week-view${repositoryOpen ? ' has-repository' : ''}`}>
          <div className="planning-week-surface">
            <WeeklyPlanningBoard nodes={[...activeNodes, ...backlogNodes]} categoryById={categoryById}
              isLoading={isNodesLoading} focusedDay={planningDay} onFocusDay={onPlanningDayChange}
              onAddTask={onAddPlannedTask} onOpenTask={handleOpenBrief} onStatusChange={onStatusChange} />
          </div>
          <article id="floating-task-repository" className="planning-surface planning-tree-surface floating-task-repository" hidden={!repositoryOpen} aria-label="Task repository">
            <header className="floating-repository-header"><h3>{workspaceName} tree</h3><button type="button" aria-label="Hide task repository" onClick={() => setRepositoryOpen(false)}><X size={16} aria-hidden="true" /></button></header>
            <div className="planning-tree-controls">
              <div className="planning-tree-search-row">
                <label className="planning-tree-search-shell">
                  <Search size={15} aria-hidden="true" />
                  <input
                    type="search"
                    value={treeSearchQuery}
                    onChange={(event) => setTreeSearchQuery(event.target.value)}
                    placeholder="Search tasks..."
                    aria-label="Search tasks in this workspace"
                  />
                </label>
                <div className="planning-tree-menu-anchor">
                  <button
                    type="button"
                    className={`planning-tree-icon-btn${hasActiveTreeFilters ? ' is-active' : ''}`}
                    aria-label={`Filter tasks. Current filters: ${treeFilterSummary}`}
                    aria-controls="planning-task-filters"
                    aria-expanded={openTreeMenu === 'filter'}
                    onClick={() => setOpenTreeMenu((current) => current === 'filter' ? null : 'filter')}
                  >
                    <Filter size={16} aria-hidden="true" />
                    {activeTreeFilterCount > 0 ? (
                      <span className="planning-tree-filter-count" aria-hidden="true">
                        {activeTreeFilterCount}
                      </span>
                    ) : null}
                  </button>
                </div>
              </div>
              {openTreeMenu === 'filter' ? (
                <section
                  id="planning-task-filters"
                  className="planning-tree-filter-shelf"
                  aria-label="Task filters"
                >
                  <div className="planning-tree-filter-heading">
                    <div>
                      <strong>Filters</strong>
                      <span>Selections stack across rows</span>
                    </div>
                    <button
                      type="button"
                      onClick={resetTreeFilters}
                      disabled={!hasActiveTreeFilters}
                    >
                      Clear all
                    </button>
                  </div>
                  <div className="planning-tree-filter-facets">
                    {TREE_FILTER_MATRIX.map(({ facet, label, options }) => {
                      const facetValue = treeFilters[facet];
                      const facetState =
                        facetValue === 'both'
                          ? 'is-any'
                          : facetValue === 'none'
                            ? 'is-none'
                            : 'is-restrictive';

                      return (
                        <div
                          key={facet}
                          className={`planning-tree-filter-facet ${facetState}`}
                          role="group"
                          aria-label={`${label} filter`}
                          data-filter-facet={facet}
                        >
                          <span className="planning-tree-filter-facet-label">{label}</span>
                          <div className="planning-tree-filter-choices">
                            {options.map(([polarity, optionLabel]) => {
                              const selected = isFilterPolaritySelected(facetValue, polarity);

                              return (
                                <label
                                  key={polarity}
                                  className={`planning-tree-filter-chip${selected ? ' is-selected' : ''}`}
                                >
                                  <input
                                    type="checkbox"
                                    checked={selected}
                                    onChange={() => toggleTreeFilterOption(facet, polarity)}
                                  />
                                  <span className="planning-tree-filter-check" aria-hidden="true">
                                    {selected ? <Check size={11} strokeWidth={2.5} /> : null}
                                  </span>
                                  <span>{optionLabel}</span>
                                </label>
                              );
                            })}
                          </div>
                          <span className="planning-tree-filter-facet-state">
                            {filterValueLabel(facet, facetValue)}
                          </span>
                        </div>
                      );
                    })}
                  </div>
                </section>
              ) : hasActiveTreeFilters ? (
                <div
                  className="planning-tree-applied-filters"
                  role="group"
                  aria-label="Applied task filters"
                >
                  {TREE_FILTER_MATRIX.map(({ facet, label }) => {
                    const facetValue = treeFilters[facet];
                    if (facetValue === 'both') {
                      return null;
                    }
                    const valueLabel = filterValueLabel(facet, facetValue);

                    return (
                      <button
                        key={facet}
                        type="button"
                        onClick={() => updateTreeFilter(facet, 'both')}
                        aria-label={`Clear ${label} filter: ${valueLabel}`}
                      >
                        <span>{facetValue === 'none' ? `${label}: None` : valueLabel}</span>
                        <X size={11} aria-hidden="true" />
                      </button>
                    );
                  })}
                  <button
                    type="button"
                    className="clear-all"
                    onClick={resetTreeFilters}
                  >
                    Clear all
                  </button>
                </div>
              ) : null}
              <div className="planning-tree-action-row">
                <button
                  type="button"
                  className="planning-panel-add-btn"
                  onClick={onAddNode}
                  disabled={isAllWorkspace}
                  title={
                    isAllWorkspace
                      ? 'Choose a specific workspace before adding a task'
                      : undefined
                  }
                >
                  <span aria-hidden="true">+</span> Add task
                </button>
                <div className="planning-tree-menu-anchor">
                  <button
                    type="button"
                    className={`planning-tree-icon-btn${treeSort !== 'manual' ? ' is-active' : ''}`}
                    aria-label={`Sort tasks. Current sort: ${treeSort}`}
                    aria-haspopup="menu"
                    aria-expanded={openTreeMenu === 'sort'}
                    onClick={() => setOpenTreeMenu((current) => current === 'sort' ? null : 'sort')}
                  >
                    <SlidersHorizontal size={16} aria-hidden="true" />
                  </button>
                  {openTreeMenu === 'sort' && (
                    <div className="planning-tree-menu align-right" role="menu" aria-label="Task sorting">
                      {TREE_SORT_OPTIONS.map(([value, label]) => (
                        <button
                          key={value}
                          type="button"
                          role="menuitemradio"
                          aria-checked={treeSort === value}
                          className={treeSort === value ? 'is-active' : ''}
                          onClick={() => {
                            updateTreeViewSettings({ sort: value });
                            setOpenTreeMenu(null);
                          }}
                        >
                          {label}
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            </div>
            <div
              className="planning-tree-column-headings"
              aria-hidden="true"
              style={
                {
                  '--tree-scrollbar-width': `${treeViewport.scrollbarWidth}px`,
                } as CSSProperties
              }
            >
              <span>Task</span>
              <span>Due</span>
              <span>Priority</span>
              <span>Est.</span>
              <span />
            </div>

            <div
              className={`planning-tree-scroll${isNodesLoading ? ' loading' : ''}${
                pointerDragState ? ' is-pointer-dragging' : ''
              }`}
              ref={treeScrollRef}
              onClickCapture={event => { if (Date.now() < suppressClickUntilRef.current) { event.preventDefault(); event.stopPropagation(); } }}
              onScroll={handleTreeScroll}
              role="tree"
              aria-label={`${workspaceName} task nodes`}
            >
              <div
                className="planning-tree-virtual-space"
                style={{
                  height: `${Math.max(totalTreeHeight, treeViewport.height)}px`,
                }}
              >
                {visibleVirtualRows.map(({ row, index }) => {
                  const isHoverDescendant =
                    row.kind === 'node' &&
                    hoveredTreeNode !== null &&
                    row.node.id !== hoveredTreeNode.id &&
                    containsNodeId(hoveredTreeNode, row.node.id);
                  const isHoverAncestor =
                    row.kind === 'node' &&
                    hoveredTreeNode !== null &&
                    row.node.id !== hoveredTreeNode.id &&
                    containsNodeId(row.node, hoveredTreeNode.id);

                  return (
                    <div
                      key={row.key}
                      className={`planning-tree-virtual-row row-${row.kind}${
                        isHoverDescendant ? ' hover-descendant' : ''
                      }${isHoverAncestor ? ' hover-ancestor' : ''}`}
                      onMouseEnter={() => {
                        if (row.kind === 'node') {
                          setHoveredTreeNodeId(row.node.id);
                        }
                      }}
                      onMouseLeave={() => {
                        if (row.kind === 'node') {
                          setHoveredTreeNodeId((current) =>
                            current === row.node.id ? null : current,
                          );
                        }
                      }}
                      style={{
                        height: `${rowLayout[index].height}px`,
                        transform: `translateY(${rowLayout[index].top}px)`,
                      }}
                    >
                    {row.kind === 'empty' && (
                      <div className="planning-empty-state planning-empty-state-inline">
                        <p className="planning-empty-state-title">
                          {hasActiveTreeFilters || treeSearchQuery.trim()
                            ? 'No matching tasks'
                            : 'No active tasks yet'}
                        </p>
                        <p className="planning-empty-state-body">
                          {hasActiveTreeFilters || treeSearchQuery.trim()
                            ? 'Adjust the stacked filters or search to see more tasks.'
                            : isAllWorkspace
                              ? 'Tasks from every workspace will appear here.'
                              : 'Add a task or move work out of backlog to populate the active tree.'}
                        </p>
                      </div>
                    )}

                    {row.kind === 'section' && (
                      <div
                        className={`planning-tree-section-row${
                          row.section === 'archived' ? ' archive' : ''
                        }`}
                      >
                        <button
                          type="button"
                          className={`planning-backlog-header${
                            row.section === 'archived' ? ' planning-archive-header' : ''
                          }`}
                          onClick={
                            row.section === 'backlog' ? onBacklogToggle : onArchivedToggle
                          }
                          aria-expanded={row.isExpanded}
                          aria-controls={
                            row.section === 'backlog'
                              ? 'planning-backlog-list'
                              : 'planning-archive-list'
                          }
                        >
                          <span
                            className={`planning-backlog-chevron${
                              row.isExpanded ? ' expanded' : ''
                            }`}
                            aria-hidden="true"
                          >
                            {'>'}
                          </span>
                          {row.label}
                          <span
                            className={`planning-backlog-count${
                              row.section === 'archived' ? ' planning-archive-count' : ''
                            }`}
                          >
                            {row.count}
                          </span>
                        </button>
                      </div>
                    )}

                    {row.kind === 'workspace' && (
                      <div
                        className="planning-tree-workspace-divider"
                        role="separator"
                        aria-label={`${row.label} workspace`}
                        style={{ '--workspace-color': row.accentColor } as CSSProperties}
                      >
                        <span>{row.label}</span>
                      </div>
                    )}

                    {row.kind === 'node' && (
                      <NodeCard
                        node={row.node}
                        accentColor={
                          isAllWorkspace
                            ? categoryById.get(row.node.categoryId)?.accentColor
                              ?? workspaceAccentColor
                            : workspaceAccentColor
                        }
                        workspaceLabel={
                          categoryById.get(row.node.categoryId)?.name
                        }
                        depth={row.depth}
                        continuationDepths={row.continuationDepths}
                        hasNextSibling={row.hasNextSibling}
                        childVisibility={row.lane}
                        pendingFocusNodeId={pendingFocusNodeId}
                        onAutoFocusHandled={onAutoFocusHandled}
                        spotlightNodeId={calendarSpotlightNodeId}
                        onStatusChange={onStatusChange}
                        onTitleChange={onTitleChange}
                        onSelect={setSelectedNodeId}
                        isSelected={effectiveSelectedNodeId === row.node.id}
                        selectedNodeId={effectiveSelectedNodeId}
                        stagedNodeIdSet={stagedNodeIdSet}
                        scheduledNodeIdSet={scheduledNodeIdSet}
                        onToggleStage={onToggleStageNode}
                        onOpenBrief={handleOpenBrief}
                        renderChildren={false}
                        dragMode="pointer"
                        isExpandedOverride={isExpandedForVirtualTree(
                          row.node,
                          expandedNodeIds,
                          effectiveSelectedNodeId,
                        )}
                        onToggleExpanded={handleToggleExpanded}
                        dropIntentOverride={
                          pointerDropTarget?.index === index
                            ? pointerDropTarget.intent
                            : null
                        }
                        isDraggingOverride={pointerDragState?.source.id === row.node.id}
                        onPointerDragStart={handlePointerDragStart}
                      />
                    )}
                    </div>
                  );
                })}
              </div>

              {pointerDragState && <div data-tree-root-drop className={`tree-root-drop${pointerDropTarget?.root ? ' is-active' : ''}`}>Move to top level</div>}
              {pointerDragState && (
                <div
                  ref={pointerDragPreviewRef}
                  className="planning-tree-drag-preview"
                  style={{
                    transform: `translate3d(${pointerDragState.x + 14}px, ${
                      pointerDragState.y + 12
                    }px, 0)`,
                  }}
                  aria-hidden="true"
                >
                  <span>{pointerDragState.node.emoji ?? 'O'}</span>
                  {pointerDragState.node.title}
                </div>
              )}
            </div>
          </article>
      </div>

      {focusNode && (
        <TaskPageDialog onClose={() => setOpenBriefNodeId(null)}>

          <article
            className="planning-surface planning-detail-surface planning-detail-modal"
            onClick={(event) => event.stopPropagation()}
          >
            <div className="planning-surface-header">
              <div>
                <textarea
                  id="planning-detail-modal-title"
                  rows={1}
                  ref={element => { if (element) { element.style.height = 'auto'; element.style.height = `${element.scrollHeight}px`; } }}
                  className="planning-detail-title-input"
                  value={currentTitleDraft}
                  onChange={(event) =>
                    setTitleDrafts((current) => ({
                      ...current,
                      [focusNode.id]: event.target.value,
                    }))
                  }
                  onBlur={commitTitle}
                  onKeyDown={handleTitleKeyDown}
                  aria-label="Task title"
                />
              </div>

              <button
                type="button"
                className="planning-detail-close-btn"
                onClick={() => setOpenBriefNodeId(null)}
                aria-label="Close task page"
                title="Close"
              >
                <X size={22} />
              </button>
            </div>

            <div className="planning-detail-scroll">
              <p className="planning-detail-breadcrumb">Notes / Specs</p>
              <textarea
                className="planning-detail-notes"
                value={currentBriefNotes}
                onChange={(event) =>
                  setBriefNotes((current) => ({
                    ...current,
                    [focusNode.id]: event.target.value,
                  }))
                }
                onBlur={() =>
                  void updateNode(focusNode.id, {
                    notes: currentBriefNotes,
                  })
                }
                placeholder="Write notes, specs, or context for this task..."
                aria-label="Task working note"
              />

              <section className="planning-detail-section">
                <div className="planning-detail-section-header">
                  <p className="planning-detail-breadcrumb">Tags</p>
                </div>

                <div className="planning-tag-editor">
                  <div className="planning-tag-list" aria-label="Task tags">
                    {focusNode.tags.length > 0 ? (
                      focusNode.tags.map((tag) => (
                        <button
                          key={tag}
                          type="button"
                          className="planning-tag-chip"
                          onClick={() => void handleRemoveTag(tag)}
                          aria-label={`Remove tag ${tag}`}
                          title="Remove tag"
                        >
                          <span>{tag}</span>
                          <span aria-hidden="true">x</span>
                        </button>
                      ))
                    ) : (
                      <span className="planning-detail-empty">No tags yet</span>
                    )}
                  </div>

                  <div className="planning-tag-input-row">
                    <input
                      type="text"
                      className="planning-detail-text-input"
                      value={currentTagDraft}
                      onChange={(event) =>
                        focusNode &&
                        setTagDrafts((current) => ({
                          ...current,
                          [focusNode.id]: event.target.value,
                        }))
                      }
                      onKeyDown={(event) => void handleTagKeyDown(event)}
                      placeholder="Add a tag"
                      aria-label="Add tag"
                    />
                    <button
                      type="button"
                      className="planning-ghost-btn"
                      onClick={() => void handleAddTag()}
                    >
                      Add tag
                    </button>
                  </div>
                </div>
              </section>

              <div className="planning-detail-grid">
                <section className="planning-detail-brief">
                  {focusChecklist.length > 0 && (
                    <div className="planning-checklist">
                      <p className="planning-checklist-title">Definition of done</p>
                      {focusChecklist.map((child) => (
                        <label key={child.id} className="planning-checklist-row">
                          <input
                            type="checkbox"
                            checked={child.status === 'done'}
                            onChange={(event) => onStatusChange(child.id, event.target.checked)}
                          />
                          <span>{child.title}</span>
                        </label>
                      ))}
                    </div>
                  )}
                </section>

                <dl className="planning-detail-facts">
                  <div>
                    <dt>Status</dt>
                    <dd>{formatStatus(focusNode.status)}</dd>
                  </div>
                  <div>
                    <dt>Planned day</dt>
                    <dd className="planning-detail-field-group">
                      <input type="date" className="planning-detail-date-input" value={focusNode.plannedDate ?? ''}
                        aria-label="Task planned day" onChange={event => void updateNode(focusNode.id, { plannedDate: event.target.value || null })} />
                      {focusNode.plannedDate && <button type="button" className="planning-detail-inline-btn subtle"
                        onClick={() => void updateNode(focusNode.id, { plannedDate: null })}>Clear</button>}
                    </dd>
                  </div>
                  <div>
                    <dt>Deadline</dt>
                    <dd className="planning-detail-field-group">
                      <input
                        type="date"
                        className="planning-detail-date-input"
                        value={focusNode.deadlineAt ?? ''}
                        onChange={(event) =>
                          void updateNode(focusNode.id, {
                            deadlineAt: event.target.value || null,
                          })
                        }
                        aria-label="Task deadline"
                      />
                      {focusNode.deadlineAt && (
                        <button
                          type="button"
                          className="planning-detail-inline-btn subtle"
                          onClick={() => void updateNode(focusNode.id, { deadlineAt: null })}
                        >
                          Clear
                        </button>
                      )}
                    </dd>
                  </div>
                  <div>
                    <dt>Priority</dt>
                    <dd>
                      <select
                        className="planning-detail-select"
                        value={focusNode.priority}
                        onChange={(event) =>
                          void updateNode(focusNode.id, {
                            priority: event.target.value as NodePriority,
                          })
                        }
                        aria-label="Task priority"
                      >
                        <option value="low">Low</option>
                        <option value="medium">Medium</option>
                        <option value="high">High</option>
                      </select>
                    </dd>
                  </div>
                  <div>
                    <dt>Estimate</dt>
                    <dd>
                      <button
                        type="button"
                        className="planning-detail-inline-btn"
                        onClick={() => {
                          const input = window.prompt(
                            'Enter time estimate in minutes:',
                            focusNode.timeEstimateMinutes?.toString() || '',
                          );
                          if (input === null) return;
                          const minutes = parseInt(input, 10);
                          if (!Number.isNaN(minutes)) {
                            void updateNode(focusNode.id, { timeEstimateMinutes: minutes });
                          }
                        }}
                      >
                        {formatTime(focusNode.timeEstimateMinutes)}
                      </button>
                    </dd>
                  </div>
                  <div>
                    <dt>Pinned</dt>
                    <dd>
                      <button
                        type="button"
                        className="planning-detail-inline-btn"
                        onClick={() =>
                          void updateNode(focusNode.id, { isPinned: !focusNode.isPinned })
                        }
                      >
                        {focusNode.isPinned ? 'Pinned' : 'Not pinned'}
                      </button>
                    </dd>
                  </div>
                  <div>
                    <dt>Subtasks</dt>
                    <dd>{focusNode.children.length}</dd>
                  </div>
                  <div>
                    <dt>Recurring</dt>
                    <dd>
                      <button
                        type="button"
                        className="planning-detail-inline-btn"
                        onClick={() => setIsRecurrenceDialogOpen(true)}
                      >
                        <RefreshCw size={14} aria-hidden="true" />
                        {formatRecurrenceSummary(focusNode.recurrenceRule)}
                      </button>
                    </dd>
                  </div>
                  <div>
                    <dt>Backlog</dt>
                    <dd>
                      <button
                        type="button"
                        className="planning-detail-inline-btn"
                        onClick={() =>
                          void updateNode(focusNode.id, { isBacklog: !focusNode.isBacklog })
                        }
                      >
                        {focusNode.isBacklog ? 'In backlog' : 'Active tree'}
                      </button>
                    </dd>
                  </div>
                </dl>
              </div>

              <section className="planning-detail-section">
                <div className="planning-detail-section-header">
                  <div>
                    <p className="planning-detail-breadcrumb">Image attachments</p>
                    <p className="planning-detail-supporting-copy">
                      Add screenshots, references, or visual context for future AI workflows.
                    </p>
                  </div>

                  <button
                    type="button"
                    className="planning-ghost-btn"
                    onClick={() => imageInputRef.current?.click()}
                    disabled={isUploadingImages}
                  >
                    {isUploadingImages ? 'Attaching...' : 'Attach images'}
                  </button>
                </div>

                <input
                  ref={imageInputRef}
                  type="file"
                  accept="image/*"
                  multiple
                  className="planning-hidden-file-input"
                  onChange={(event) => void handleImageInputChange(event)}
                />

                {focusNode.imageAttachments.length > 0 ? (
                  <div className="planning-attachment-grid">
                    {focusNode.imageAttachments.map((attachment) => (
                      <figure key={attachment.id} className="planning-attachment-card">
                        <img
                          src={attachment.dataUrl}
                          alt={attachment.name}
                          className="planning-attachment-image"
                        />
                        <figcaption className="planning-attachment-footer">
                          <span title={attachment.name}>{attachment.name}</span>
                          <button
                            type="button"
                            className="planning-detail-inline-btn subtle"
                            onClick={() => void handleRemoveAttachment(attachment.id)}
                          >
                            Remove
                          </button>
                        </figcaption>
                      </figure>
                    ))}
                  </div>
                ) : (
                  <div className="planning-detail-empty-surface">
                    No images attached yet.
                  </div>
                )}
              </section>
            </div>
          </article>
          {isRecurrenceDialogOpen && <Suspense fallback={null}><RecurrenceRuleDialog node={focusNode} onClose={() => setIsRecurrenceDialogOpen(false)} /></Suspense>}
        </TaskPageDialog>
      )}
      {workspaceDialog && createPortal(
        <div
          className="workspace-dialog-backdrop"
          role="presentation"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) {
              closeWorkspaceDialog();
            }
          }}
        >
          {workspaceDialog.mode === 'rename' ? (
            <form
              className="workspace-dialog"
              role="dialog"
              aria-modal="true"
              aria-labelledby={`workspace-rename-title-${workspaceDialog.target.id}`}
              onSubmit={(event) => {
                event.preventDefault();
                void renameWorkspace();
              }}
            >
              <div className="workspace-dialog-heading">
                <span className="workspace-dialog-icon" aria-hidden="true">
                  <Pencil size={19} />
                </span>
                <div>
                  <p>Workspace settings</p>
                  <h2 id={`workspace-rename-title-${workspaceDialog.target.id}`}>
                    Rename “{workspaceDialog.target.name}”
                  </h2>
                </div>
              </div>

              <label className="workspace-dialog-field">
                <span>Workspace name</span>
                <input
                  autoFocus
                  value={workspaceNameDraft}
                  onChange={(event) => setWorkspaceNameDraft(event.target.value)}
                  aria-label="Workspace name"
                  autoComplete="off"
                  disabled={isWorkspaceActionPending}
                />
              </label>

              <div className="workspace-dialog-actions">
                <button
                  type="button"
                  onClick={closeWorkspaceDialog}
                  disabled={isWorkspaceActionPending}
                >
                  <X size={16} aria-hidden="true" />
                  Cancel
                </button>
                <button
                  type="submit"
                  className="primary"
                  disabled={!workspaceNameDraft.trim() || isWorkspaceActionPending}
                >
                  <Pencil size={16} aria-hidden="true" />
                  {isWorkspaceActionPending ? 'Saving…' : 'Save name'}
                </button>
              </div>
            </form>
          ) : (
            <section
              className="workspace-dialog danger"
              role="dialog"
              aria-modal="true"
              aria-labelledby={`workspace-delete-title-${workspaceDialog.target.id}`}
              aria-describedby={`workspace-delete-description-${workspaceDialog.target.id}`}
            >
              <div className="workspace-dialog-heading">
                <span className="workspace-dialog-icon danger" aria-hidden="true">
                  <TriangleAlert size={20} />
                </span>
                <div>
                  <p>Permanent workspace deletion</p>
                  <h2 id={`workspace-delete-title-${workspaceDialog.target.id}`}>
                    {deleteStep === 1
                      ? `Delete “${workspaceDialog.target.name}”?`
                      : `Type “${workspaceDialog.target.name}” to confirm`}
                  </h2>
                </div>
              </div>

              {deleteStep === 1 ? (
                <p
                  className="workspace-dialog-copy"
                  id={`workspace-delete-description-${workspaceDialog.target.id}`}
                >
                  This permanently deletes the workspace and all of its tasks, calendar blocks,
                  and recurrence rules. This cannot be undone.
                </p>
              ) : (
                <div id={`workspace-delete-description-${workspaceDialog.target.id}`}>
                  <p className="workspace-dialog-copy">
                    Enter the exact workspace name below. This is the second and final
                    confirmation.
                  </p>
                  <label className="workspace-dialog-field">
                    <span>Workspace name</span>
                    <input
                      autoFocus
                      value={deleteConfirmation}
                      onChange={(event) => setDeleteConfirmation(event.target.value)}
                      aria-label={`Type ${workspaceDialog.target.name} to confirm deletion`}
                      autoComplete="off"
                      spellCheck={false}
                      disabled={isWorkspaceActionPending}
                    />
                  </label>
                </div>
              )}

              <div className="workspace-dialog-actions">
                <button
                  type="button"
                  autoFocus={deleteStep === 1}
                  onClick={deleteStep === 1 ? closeWorkspaceDialog : () => {
                    setDeleteStep(1);
                    setDeleteConfirmation('');
                  }}
                  disabled={isWorkspaceActionPending}
                >
                  <X size={16} aria-hidden="true" />
                  {deleteStep === 1 ? 'Cancel' : 'Back'}
                </button>
                {deleteStep === 1 ? (
                  <button
                    type="button"
                    className="danger"
                    onClick={() => setDeleteStep(2)}
                  >
                    Continue to final confirmation
                  </button>
                ) : (
                  <button
                    type="button"
                    className="danger"
                    onClick={() => void deleteWorkspace()}
                    disabled={
                      deleteConfirmation !== workspaceDialog.target.name ||
                      isWorkspaceActionPending
                    }
                  >
                    <Trash2 size={16} aria-hidden="true" />
                    {isWorkspaceActionPending ? 'Deleting…' : 'Delete workspace permanently'}
                  </button>
                )}
              </div>
            </section>
          )}
        </div>,
        document.body,
      )}

    </section>
  );
}
