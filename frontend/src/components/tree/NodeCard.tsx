import { lazy, memo, Suspense, useState, useRef, useCallback, useEffect, useMemo } from 'react';
import { createPortal } from 'react-dom';
import type {
  CSSProperties,
  ChangeEvent,
  DragEvent,
  KeyboardEvent,
  MouseEvent as ReactMouseEvent,
  PointerEvent as ReactPointerEvent,
  UIEvent,
} from 'react';
import {
  Archive,
  ArchiveRestore,
  CalendarCheck2,
  ListChecks,
  RefreshCw,
  Trash2,
  TriangleAlert,
  X,
} from 'lucide-react';
import type { NodeDragPayload, StrategyNode } from '../../types/node';
import { useNodeStore } from '../../store/useNodeStore';
import { suggestEmoji } from '../../lib/emoji';
import { isArchivedNode, visibleChildrenForLane } from '../../lib/nodeState';
import { formatRecurrenceSummary } from '../../lib/recurrence';
import './NodeCard.css';

const EmojiPicker = lazy(() =>
  import('emoogle-emoji-picker').then((module) => ({ default: module.EmojiPicker })),
);
const RecurrenceRuleDialog = lazy(() =>
  import('./RecurrenceRuleDialog').then((module) => ({
    default: module.RecurrenceRuleDialog,
  })),
);

interface NodeCardProps {
  node: StrategyNode;
  accentColor?: string;
  workspaceLabel?: string;
  depth?: number;
  continuationDepths?: number[];
  childVisibility?: 'active' | 'backlog' | 'archived';
  onStatusChange?: (id: string, done: boolean) => void;
  onTitleChange?: (id: string, title: string) => void;
  pendingFocusNodeId?: string | null;
  onAutoFocusHandled?: () => void;
  spotlightNodeId?: string | null;
  onSelect?: (id: string) => void;
  isSelected?: boolean;
  selectedNodeId?: string | null;
  stagedNodeIdSet?: Set<string>;
  scheduledNodeIdSet?: Set<string>;
  onToggleStage?: (id: string) => void;
  onOpenBrief?: (id: string) => void;
  renderChildren?: boolean;
  dragMode?: 'native' | 'pointer';
  isExpandedOverride?: boolean;
  onToggleExpanded?: (id: string) => void;
  dropIntentOverride?: DropIntent;
  isDraggingOverride?: boolean;
  onPointerDragStart?: (node: StrategyNode, event: ReactPointerEvent<HTMLDivElement>) => void;
}

type DropIntent = 'before' | 'inside' | 'after' | null;
type PickerInput = HTMLInputElement & { showPicker?: () => void };
const ESTIMATE_WHEEL_ITEM_HEIGHT = 40;
const ESTIMATE_HOUR_OPTIONS = Array.from({ length: 13 }, (_, index) => index);
const ESTIMATE_MINUTE_OPTIONS = Array.from({ length: 60 }, (_, index) => index);
const PRIORITY_OPTIONS: StrategyNode['priority'][] = ['low', 'medium', 'high'];

function formatTime(minutes?: number | null): string | null {
  if (!minutes) return null;
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const remainder = minutes % 60;
  return remainder ? `${hours}h ${remainder}m` : `${hours}h`;
}

function getStatusLabel(node: StrategyNode) {
  if (isArchivedNode(node)) return 'Archived';
  if (node.isBacklog) return 'Backlog';
  if (node.status === 'done') return 'Done';
  if (node.status === 'in-progress') return 'In progress';
  if (node.children.length > 0) return 'Parent task';
  return 'Task';
}

function formatDeadline(deadlineAt?: string | null) {
  if (!deadlineAt) return null;
  const parsedDate = new Date(`${deadlineAt}T00:00:00`);
  if (Number.isNaN(parsedDate.getTime())) {
    return null;
  }

  return new Intl.DateTimeFormat('en-US', {
    month: 'short',
    day: 'numeric',
  }).format(parsedDate);
}

function formatEstimateDraft(minutes: number) {
  if (minutes <= 0) {
    return 'No estimate';
  }

  const hours = Math.floor(minutes / 60);
  const remainder = minutes % 60;

  if (hours === 0) {
    return `${remainder}m`;
  }
  if (remainder === 0) {
    return `${hours}h`;
  }

  return `${hours}h ${remainder}m`;
}

function formatWheelValue(value: number) {
  return value.toString().padStart(2, '0');
}

function CalendarMetaIcon() {
  return (
    <svg viewBox="0 0 20 20" className="node-meta-icon" aria-hidden="true">
      <rect x="3.5" y="4.5" width="13" height="12" rx="2.5" />
      <path d="M3.5 8h13M7 3.5v3M13 3.5v3" />
    </svg>
  );
}

function TimerMetaIcon() {
  return (
    <svg viewBox="0 0 20 20" className="node-meta-icon" aria-hidden="true">
      <circle cx="10" cy="11" r="5.5" />
      <path d="M10 11V8.5M10 11l2 1.5M8 3.5h4" />
    </svg>
  );
}

function StageMetaIcon({ active = false }: { active?: boolean }) {
  return (
    <svg viewBox="0 0 20 20" className="node-meta-icon" aria-hidden="true">
      <circle cx="10" cy="10" r="6.5" />
      {active ? <path d="M7.4 10.2 9.2 12l3.6-3.8" /> : <path d="M10 7v6M7 10h6" />}
    </svg>
  );
}

function priorityTone(priority: StrategyNode['priority']) {
  if (priority === 'high') return 'red';
  if (priority === 'low') return 'green';
  return 'blue';
}

function formatPriorityLabel(priority: StrategyNode['priority']) {
  if (priority === 'high') return 'High';
  if (priority === 'low') return 'Low';
  return 'Medium';
}

function readDragPayload(
  event: DragEvent<HTMLDivElement> | DragEvent,
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

function readInternalDragPayload(): NodeDragPayload | null {
  return useNodeStore.getState().activeDragPayload;
}

function getReorderIndex(
  dropIntent: 'before' | 'after',
  node: StrategyNode,
  source: NodeDragPayload,
): number {
  let targetIndex = (node.sortOrder ?? 0) + (dropIntent === 'after' ? 1 : 0);
  const parentId = node.parentId ?? null;

  if (source.parentId === parentId) {
    const sourceIndex = source.sortOrder;
    if (sourceIndex < targetIndex) {
      targetIndex -= 1;
    }
  }

  return targetIndex;
}

export const NodeCard = memo(function NodeCard({
  node,
  accentColor = '#6366f1',
  workspaceLabel,
  depth = 0,
  continuationDepths = [],
  childVisibility = 'active',
  onStatusChange,
  onTitleChange,
  pendingFocusNodeId = null,
  onAutoFocusHandled,
  spotlightNodeId = null,
  onSelect,
  isSelected = false,
  selectedNodeId = null,
  stagedNodeIdSet,
  scheduledNodeIdSet,
  onToggleStage,
  onOpenBrief,
  renderChildren = true,
  dragMode = 'native',
  isExpandedOverride,
  onToggleExpanded,
  dropIntentOverride = null,
  isDraggingOverride,
  onPointerDragStart,
}: NodeCardProps) {
  const [isExpanded, setIsExpanded] = useState(true);
  const [isDragging, setIsDragging] = useState(false);
  const [isEditing, setIsEditing] = useState(false);
  const [isEmojiPickerOpen, setIsEmojiPickerOpen] = useState(false);
  const [isDeadlinePickerOpen, setIsDeadlinePickerOpen] = useState(false);
  const [isEstimatePickerOpen, setIsEstimatePickerOpen] = useState(false);
  const [isPriorityPickerOpen, setIsPriorityPickerOpen] = useState(false);
  const [deadlinePopoverPosition, setDeadlinePopoverPosition] = useState<{
    left: number;
    top: number;
  } | null>(null);
  const [estimatePopoverPosition, setEstimatePopoverPosition] = useState<{
    left: number;
    top: number;
  } | null>(null);
  const [priorityPopoverPosition, setPriorityPopoverPosition] = useState<{
    left: number;
    top: number;
  } | null>(null);
  const [estimateDraftMinutes, setEstimateDraftMinutes] = useState(
    node.timeEstimateMinutes ?? 0,
  );
  const [contextMenuPosition, setContextMenuPosition] = useState<{
    x: number;
    y: number;
  } | null>(null);
  const [isDeleteConfirmOpen, setIsDeleteConfirmOpen] = useState(false);
  const [isRecurrenceDialogOpen, setIsRecurrenceDialogOpen] = useState(false);
  const [isDeleting, setIsDeleting] = useState(false);
  const [dropIntent, setDropIntent] = useState<DropIntent>(null);
  const isScheduled = scheduledNodeIdSet?.has(node.id) ?? false;

  const deleteNode = useNodeStore((state) => state.deleteNode);
  const addNode = useNodeStore((state) => state.addNode);
  const updateNode = useNodeStore((state) => state.updateNode);
  const moveNode = useNodeStore((state) => state.moveNode);
  const setActiveDragPayload = useNodeStore((state) => state.setActiveDragPayload);
  const cardRef = useRef<HTMLDivElement>(null);
  const rowRef = useRef<HTMLDivElement>(null);
  const titleRef = useRef<HTMLSpanElement>(null);
  const pickerRef = useRef<HTMLDivElement>(null);
  const deadlineControlRef = useRef<HTMLDivElement>(null);
  const deadlineInputRef = useRef<HTMLInputElement>(null);
  const deadlinePopoverRef = useRef<HTMLDivElement>(null);
  const priorityControlRef = useRef<HTMLDivElement>(null);
  const priorityPopoverRef = useRef<HTMLDivElement>(null);
  const estimateControlRef = useRef<HTMLDivElement>(null);
  const estimateHoursWheelRef = useRef<HTMLDivElement>(null);
  const estimateMinutesWheelRef = useRef<HTMLDivElement>(null);
  const estimatePopoverRef = useRef<HTMLDivElement>(null);
  const contextMenuRef = useRef<HTMLDivElement>(null);
  const titleClickTimerRef = useRef<number | null>(null);
  const dropIntentRef = useRef<DropIntent>(null);
  const isCompleted = node.status === 'done';
  const isArchived = isArchivedNode(node);
  const shouldAutoFocusTitle = pendingFocusNodeId === node.id;
  const visibleChildren = useMemo(
    () => visibleChildrenForLane(node, childVisibility),
    [childVisibility, node],
  );
  const hasVisibleChildren = visibleChildren.length > 0;
  const descendantCount = useMemo(() => countDescendants(node.children), [node.children]);
  const isRecurring = node.isRecurring;
  const timeLabel = formatTime(node.timeEstimateMinutes);
  const statusLabel = getStatusLabel(node);
  const displayEmoji = node.emoji || suggestEmoji(node.title, node.type);
  const deadlineLabel = formatDeadline(node.deadlineAt);
  const priorityLabel = formatPriorityLabel(node.priority);
  const estimateHours = Math.floor(estimateDraftMinutes / 60);
  const estimateMinutes = estimateDraftMinutes % 60;
  const isExecutionSelected = stagedNodeIdSet?.has(node.id) ?? false;
  const isCalendarSpotlighted = spotlightNodeId === node.id;
  const stagedDescendantCount = useMemo(
    () =>
      stagedNodeIdSet
        ? countStagedDescendants(visibleChildren, stagedNodeIdSet, childVisibility)
        : 0,
    [childVisibility, stagedNodeIdSet, visibleChildren],
  );
  const hasStagedDescendant = stagedDescendantCount > 0;
  const displayDropIntent = dropIntentOverride ?? dropIntent;
  const displayDragging = isDraggingOverride ?? isDragging;

  const openNativePicker = (input: HTMLInputElement | null) => {
    if (!input) {
      return;
    }

    window.requestAnimationFrame(() => {
      input.focus();
      (input as PickerInput).showPicker?.();
    });
  };

  const startEdit = useCallback(() => {
    setIsEditing(true);
    window.setTimeout(() => {
      if (!titleRef.current) {
        return;
      }

      titleRef.current.focus();
      const range = document.createRange();
      range.selectNodeContents(titleRef.current);
      range.collapse(false);
      window.getSelection()?.removeAllRanges();
      window.getSelection()?.addRange(range);
    }, 0);
  }, []);

  useEffect(() => {
    if (!shouldAutoFocusTitle) {
      return;
    }

    const timer = window.setTimeout(() => {
      startEdit();
      onAutoFocusHandled?.();
    }, 0);

    return () => {
      window.clearTimeout(timer);
    };
  }, [onAutoFocusHandled, shouldAutoFocusTitle, startEdit]);

  useEffect(() => {
    if (!isEmojiPickerOpen) {
      return;
    }

    const handlePointerDown = (event: MouseEvent) => {
      const target = event.target as Node;
      if (pickerRef.current?.contains(target) || cardRef.current?.contains(target)) {
        return;
      }
      setIsEmojiPickerOpen(false);
    };

    document.addEventListener('mousedown', handlePointerDown);
    return () => {
      document.removeEventListener('mousedown', handlePointerDown);
    };
  }, [isEmojiPickerOpen]);

  useEffect(() => {
    if (!isDeadlinePickerOpen && !isEstimatePickerOpen && !isPriorityPickerOpen) {
      return;
    }

    const handlePointerDown = (event: MouseEvent) => {
      const target = event.target as Node;
      if (deadlineControlRef.current?.contains(target)) {
        return;
      }
      if (deadlinePopoverRef.current?.contains(target)) {
        return;
      }
      if (priorityControlRef.current?.contains(target)) {
        return;
      }
      if (priorityPopoverRef.current?.contains(target)) {
        return;
      }
      if (estimateControlRef.current?.contains(target)) {
        return;
      }
      if (estimatePopoverRef.current?.contains(target)) {
        return;
      }
      setIsDeadlinePickerOpen(false);
      setIsEstimatePickerOpen(false);
      setIsPriorityPickerOpen(false);
      setDeadlinePopoverPosition(null);
      setEstimatePopoverPosition(null);
      setPriorityPopoverPosition(null);
    };

    document.addEventListener('mousedown', handlePointerDown);
    return () => {
      document.removeEventListener('mousedown', handlePointerDown);
    };
  }, [isDeadlinePickerOpen, isEstimatePickerOpen, isPriorityPickerOpen]);

  useEffect(() => {
    if (!isDeadlinePickerOpen) {
      return;
    }

    openNativePicker(deadlineInputRef.current);
  }, [isDeadlinePickerOpen]);

  useEffect(() => {
    if (!contextMenuPosition) {
      return;
    }

    const handlePointerDown = (event: MouseEvent) => {
      const target = event.target as Node;
      if (contextMenuRef.current?.contains(target)) {
        return;
      }
      setContextMenuPosition(null);
    };

    const handleContextMenu = (event: MouseEvent) => {
      const target = event.target as Node;
      if (contextMenuRef.current?.contains(target)) {
        return;
      }
      setContextMenuPosition(null);
    };

    document.addEventListener('mousedown', handlePointerDown);
    document.addEventListener('contextmenu', handleContextMenu, true);
    return () => {
      document.removeEventListener('mousedown', handlePointerDown);
      document.removeEventListener('contextmenu', handleContextMenu, true);
    };
  }, [contextMenuPosition]);

  useEffect(() => {
    if (!isDeleteConfirmOpen) {
      return;
    }

    const handleKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key === 'Escape' && !isDeleting) {
        setIsDeleteConfirmOpen(false);
      }
    };

    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [isDeleteConfirmOpen, isDeleting]);

  useEffect(() => {
    if (!isEstimatePickerOpen) {
      return;
    }

    window.requestAnimationFrame(() => {
      estimateHoursWheelRef.current?.scrollTo({
        top: estimateHours * ESTIMATE_WHEEL_ITEM_HEIGHT,
        behavior: 'auto',
      });
      estimateMinutesWheelRef.current?.scrollTo({
        top: estimateMinutes * ESTIMATE_WHEEL_ITEM_HEIGHT,
        behavior: 'auto',
      });
    });
  }, [estimateHours, estimateMinutes, isEstimatePickerOpen]);

  useEffect(() => {
    return () => {
      if (titleClickTimerRef.current !== null) {
        window.clearTimeout(titleClickTimerRef.current);
      }
    };
  }, []);

  const containsSelectedDescendant = useMemo(
    () =>
      !!selectedNodeId &&
      selectedNodeId !== node.id &&
      hasVisibleChildren &&
      visibleChildren.some((child) => containsNodeId(child, selectedNodeId)),
    [hasVisibleChildren, node.id, selectedNodeId, visibleChildren],
  );
  const isExpandedVisible = isExpandedOverride ?? (isExpanded || containsSelectedDescendant);

  const handleDragStart = (event: DragEvent) => {
    if (
      event.target instanceof HTMLElement &&
      event.target.closest('button, input, textarea, select, .node-title')
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
    setActiveDragPayload(payload);
    setIsDragging(true);
    setContextMenuPosition(null);
    setIsDeadlinePickerOpen(false);
    setIsEstimatePickerOpen(false);
    setIsPriorityPickerOpen(false);
    setDeadlinePopoverPosition(null);
    setEstimatePopoverPosition(null);
    setPriorityPopoverPosition(null);
    setIsEmojiPickerOpen(false);
    const serializedPayload = JSON.stringify(payload);
    event.dataTransfer.setData('application/x-drasil-node', serializedPayload);
    event.dataTransfer.setData('text/plain', serializedPayload);
    event.dataTransfer.effectAllowed = 'copyMove';
  };

  const handleDragEnd = (event: DragEvent) => {
    event.stopPropagation();
    setIsDragging(false);
    updateDropIntent(null);
    window.requestAnimationFrame(() => {
      setActiveDragPayload(null);
    });
  };

  const handleCheckboxChange = (checked: boolean) => {
    onStatusChange?.(node.id, checked);
  };

  const commitEdit = useCallback(() => {
    if (!titleRef.current) {
      return;
    }

    const newTitle = titleRef.current.textContent?.trim() || node.title;
    titleRef.current.textContent = newTitle;
    setIsEditing(false);
    if (newTitle !== node.title) {
      onTitleChange?.(node.id, newTitle);
    }
  }, [node.id, node.title, onTitleChange]);

  const cancelEdit = useCallback(() => {
    if (titleRef.current) {
      titleRef.current.textContent = node.title;
    }
    setIsEditing(false);
  }, [node.title]);

  const focusFirstVisibleChild = () => {
    const firstChild = cardRef.current?.parentElement?.querySelector(
      '.node-children > .node-card-wrapper > .node-card',
    ) as HTMLElement | null;
    firstChild?.focus();
  };

  const focusParentCard = () => {
    const parentWrapper = cardRef.current?.parentElement
      ?.closest('.node-children')
      ?.closest('.node-card-wrapper');
    const parentCard = parentWrapper?.querySelector('.node-card') as HTMLElement | null;
    parentCard?.focus();
  };

  const getVisibleTreeCards = () => {
    const treeRoot = cardRef.current?.closest('[role="tree"]');
    if (!treeRoot) {
      return [];
    }

    return Array.from(treeRoot.querySelectorAll<HTMLElement>('.node-card[role="treeitem"]'));
  };

  const focusRelativeCard = (offset: number) => {
    if (!cardRef.current) {
      return;
    }

    const visibleCards = getVisibleTreeCards();
    const currentIndex = visibleCards.indexOf(cardRef.current);
    if (currentIndex < 0) {
      return;
    }

    visibleCards[currentIndex + offset]?.focus();
  };

  const focusBoundaryCard = (boundary: 'first' | 'last') => {
    const visibleCards = getVisibleTreeCards();
    if (visibleCards.length === 0) {
      return;
    }

    const targetCard =
      boundary === 'first' ? visibleCards[0] : visibleCards[visibleCards.length - 1];
    targetCard.focus();
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (isEditing) {
      if (event.key === 'Enter') {
        event.preventDefault();
        commitEdit();
      }
      if (event.key === 'Escape') {
        event.preventDefault();
        cancelEdit();
      }
      return;
    }

    switch (event.key) {
      case 'Enter':
        event.preventDefault();
        onSelect?.(node.id);
        onOpenBrief?.(node.id);
        break;
      case 'F2':
        event.preventDefault();
        onSelect?.(node.id);
        startEdit();
        break;
      case 'ArrowRight':
        event.preventDefault();
        if (hasVisibleChildren && !isExpanded) {
          setIsExpanded(true);
          break;
        }
        if (hasVisibleChildren && isExpandedVisible) {
          focusFirstVisibleChild();
        }
        break;
      case 'ArrowLeft':
        event.preventDefault();
        if (hasVisibleChildren && isExpandedVisible) {
          setIsExpanded(false);
          break;
        }
        focusParentCard();
        break;
      case 'ArrowDown':
        event.preventDefault();
        focusRelativeCard(1);
        break;
      case 'ArrowUp':
        event.preventDefault();
        focusRelativeCard(-1);
        break;
      case 'Home':
        event.preventDefault();
        focusBoundaryCard('first');
        break;
      case 'End':
        event.preventDefault();
        focusBoundaryCard('last');
        break;
      case ' ':
        event.preventDefault();
        handleCheckboxChange(!isCompleted);
        break;
      case 'ContextMenu':
        openContextMenu(event as unknown as ReactMouseEvent<HTMLDivElement>, true);
        break;
      default:
        if (event.shiftKey && event.key === 'F10') {
          openContextMenu(event as unknown as ReactMouseEvent<HTMLDivElement>, true);
        }
        break;
    }
  };

  const handleEmojiButtonClick = (event: ReactMouseEvent<HTMLButtonElement>) => {
    event.stopPropagation();
    setIsDeadlinePickerOpen(false);
    setIsEstimatePickerOpen(false);
    setIsPriorityPickerOpen(false);
    setDeadlinePopoverPosition(null);
    setEstimatePopoverPosition(null);
    setPriorityPopoverPosition(null);
    setContextMenuPosition(null);
    setIsEmojiPickerOpen((value) => !value);
  };

  const handleTimeEdit = (event: ReactMouseEvent<HTMLButtonElement>) => {
    event.stopPropagation();
    setIsDeadlinePickerOpen(false);
    setIsPriorityPickerOpen(false);
    setDeadlinePopoverPosition(null);
    setPriorityPopoverPosition(null);
    setContextMenuPosition(null);
    if (isEstimatePickerOpen) {
      setIsEstimatePickerOpen(false);
      setEstimatePopoverPosition(null);
      return;
    }

    if (!isEstimatePickerOpen) {
      setEstimateDraftMinutes(node.timeEstimateMinutes ?? 0);
      if (estimateControlRef.current) {
        setEstimatePopoverPosition(
          buildMetaPopoverPosition(estimateControlRef.current, 236, 376),
        );
      }
    }
    setIsEstimatePickerOpen(true);
  };

  const handlePriorityEdit = (event: ReactMouseEvent<HTMLButtonElement>) => {
    event.stopPropagation();
    setIsEmojiPickerOpen(false);
    setIsDeadlinePickerOpen(false);
    setIsEstimatePickerOpen(false);
    setDeadlinePopoverPosition(null);
    setEstimatePopoverPosition(null);
    setContextMenuPosition(null);
    if (isPriorityPickerOpen) {
      setIsPriorityPickerOpen(false);
      setPriorityPopoverPosition(null);
      return;
    }

    if (priorityControlRef.current) {
      setPriorityPopoverPosition(
        buildMetaPopoverPosition(priorityControlRef.current, 188, 170),
      );
    }
    setIsPriorityPickerOpen(true);
  };

  const handleDeadlineEdit = (event: ReactMouseEvent<HTMLButtonElement>) => {
    event.stopPropagation();
    setIsEstimatePickerOpen(false);
    setIsPriorityPickerOpen(false);
    setEstimatePopoverPosition(null);
    setPriorityPopoverPosition(null);
    setContextMenuPosition(null);
    if (isDeadlinePickerOpen) {
      setIsDeadlinePickerOpen(false);
      setDeadlinePopoverPosition(null);
      return;
    }

    if (!isDeadlinePickerOpen && deadlineControlRef.current) {
      setDeadlinePopoverPosition(
        buildMetaPopoverPosition(deadlineControlRef.current, 204, 146),
      );
    }
    setIsDeadlinePickerOpen(true);
  };

  const handlePrioritySelect =
    (priority: StrategyNode['priority']) => (event: ReactMouseEvent<HTMLButtonElement>) => {
      event.stopPropagation();
      void updateNode(node.id, { priority });
      setIsPriorityPickerOpen(false);
      setPriorityPopoverPosition(null);
    };

  const handleDeadlineInputChange = (event: ChangeEvent<HTMLInputElement>) => {
    const nextDeadline = event.target.value || null;
    void updateNode(node.id, { deadlineAt: nextDeadline });
  };

  const handleDeadlineClear = (event: ReactMouseEvent<HTMLButtonElement>) => {
    event.stopPropagation();
    void updateNode(node.id, { deadlineAt: null });
    setIsDeadlinePickerOpen(false);
    setDeadlinePopoverPosition(null);
  };

  const handleEstimateClear = (event: ReactMouseEvent<HTMLButtonElement>) => {
    event.stopPropagation();
    void updateNode(node.id, { timeEstimateMinutes: null });
    setIsEstimatePickerOpen(false);
    setEstimatePopoverPosition(null);
  };

  const setEstimateHours = (hours: number) => {
    setEstimateDraftMinutes((current) => hours * 60 + (current % 60));
  };

  const setEstimateMinutes = (minutes: number) => {
    setEstimateDraftMinutes((current) => Math.floor(current / 60) * 60 + minutes);
  };

  const handleEstimateHoursScroll = (event: UIEvent<HTMLDivElement>) => {
    const nextHours = Math.max(
      0,
      Math.min(
        ESTIMATE_HOUR_OPTIONS.length - 1,
        Math.round(event.currentTarget.scrollTop / ESTIMATE_WHEEL_ITEM_HEIGHT),
      ),
    );
    setEstimateHours(nextHours);
  };

  const handleEstimateMinutesScroll = (event: UIEvent<HTMLDivElement>) => {
    const nextMinutes = Math.max(
      0,
      Math.min(
        ESTIMATE_MINUTE_OPTIONS.length - 1,
        Math.round(event.currentTarget.scrollTop / ESTIMATE_WHEEL_ITEM_HEIGHT),
      ),
    );
    setEstimateMinutes(nextMinutes);
  };

  const handleEstimateHoursClick =
    (hours: number) => (event: ReactMouseEvent<HTMLButtonElement>) => {
      event.stopPropagation();
      setEstimateHours(hours);
      estimateHoursWheelRef.current?.scrollTo({
        top: hours * ESTIMATE_WHEEL_ITEM_HEIGHT,
        behavior: 'smooth',
      });
    };

  const handleEstimateMinutesClick =
    (minutes: number) => (event: ReactMouseEvent<HTMLButtonElement>) => {
      event.stopPropagation();
      setEstimateMinutes(minutes);
      estimateMinutesWheelRef.current?.scrollTo({
        top: minutes * ESTIMATE_WHEEL_ITEM_HEIGHT,
        behavior: 'smooth',
      });
    };

  const handleEstimateSave = (event: ReactMouseEvent<HTMLButtonElement>) => {
    event.stopPropagation();
    void updateNode(node.id, {
      timeEstimateMinutes: estimateDraftMinutes > 0 ? estimateDraftMinutes : null,
    });
    setIsEstimatePickerOpen(false);
    setEstimatePopoverPosition(null);
  };

  const handleStageToggle = (event: ReactMouseEvent<HTMLButtonElement>) => {
    event.stopPropagation();
    onToggleStage?.(node.id);
  };

  const buildContextMenuPosition = (clientX: number, clientY: number) => {
    const viewportPadding = 12;
    const menuWidth = 196;
    const menuHeight = 168;
    const x = Math.min(
      Math.max(clientX, viewportPadding),
      Math.max(window.innerWidth - menuWidth - viewportPadding, viewportPadding),
    );
    const fitsBelow = clientY + menuHeight + viewportPadding <= window.innerHeight;
    const y = fitsBelow
      ? Math.max(clientY + 6, viewportPadding)
      : Math.max(clientY - menuHeight - 6, viewportPadding);

    return { x, y };
  };

  function buildMetaPopoverPosition(
    control: HTMLDivElement,
    width: number,
    height: number,
  ) {
    const rect = control.getBoundingClientRect();
    const viewportPadding = 12;
    const left = Math.min(
      Math.max(rect.right - width, viewportPadding),
      window.innerWidth - width - viewportPadding,
    );
    const spaceBelow = window.innerHeight - rect.bottom;
    const top =
      spaceBelow >= height + viewportPadding
        ? rect.bottom + 8
        : Math.max(rect.top - height - 8, viewportPadding);

    return { left, top };
  }

  const openContextMenu = (
    event: ReactMouseEvent<HTMLDivElement>,
    fallbackToRowEdge = false,
  ) => {
    event.preventDefault();
    event.stopPropagation();
    setIsEmojiPickerOpen(false);
    setIsDeadlinePickerOpen(false);
    setIsEstimatePickerOpen(false);
    setIsPriorityPickerOpen(false);
    setDeadlinePopoverPosition(null);
    setEstimatePopoverPosition(null);
    setPriorityPopoverPosition(null);
    onSelect?.(node.id);

    const rowRect =
      rowRef.current?.getBoundingClientRect() ?? cardRef.current?.getBoundingClientRect();
    const clientX = fallbackToRowEdge && rowRect ? rowRect.right - 20 : event.clientX;
    const clientY = fallbackToRowEdge && rowRect ? rowRect.bottom - 4 : event.clientY;
    setContextMenuPosition(buildContextMenuPosition(clientX, clientY));
  };

  const handleContextMenuAction =
    (action: 'stage' | 'recurring' | 'backlog' | 'delete') =>
    (event: ReactMouseEvent<HTMLButtonElement>) => {
      event.stopPropagation();
      setContextMenuPosition(null);

      if (action === 'stage') {
        onToggleStage?.(node.id);
        return;
      }

      if (action === 'recurring') {
        setIsRecurrenceDialogOpen(true);
        return;
      }

      if (action === 'backlog') {
        void updateNode(node.id, { isBacklog: !node.isBacklog });
        return;
      }

      setIsDeleteConfirmOpen(true);
    };

  const confirmDelete = async () => {
    setIsDeleting(true);
    try {
      await deleteNode(node.id);
    } catch {
      setIsDeleting(false);
    }
  };

  const handleTitleClick = (event: ReactMouseEvent<HTMLSpanElement>) => {
    event.stopPropagation();
    if (titleClickTimerRef.current !== null) {
      window.clearTimeout(titleClickTimerRef.current);
    }

    titleClickTimerRef.current = window.setTimeout(() => {
      onOpenBrief?.(node.id);
      titleClickTimerRef.current = null;
    }, 180);
  };

  const handleTitleDoubleClick = (event: ReactMouseEvent<HTMLSpanElement>) => {
    event.stopPropagation();
    if (titleClickTimerRef.current !== null) {
      window.clearTimeout(titleClickTimerRef.current);
      titleClickTimerRef.current = null;
    }
    startEdit();
  };

  const handleDragOver = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    event.stopPropagation();
    event.dataTransfer.dropEffect = 'move';
  };

  const handleDragLeave = (event: DragEvent<HTMLDivElement>) => {
    event.stopPropagation();
    if (!event.currentTarget.contains(event.relatedTarget as Node | null)) {
      updateDropIntent(null);
    }
  };

  const updateDropIntent = (nextDropIntent: DropIntent) => {
    if (dropIntentRef.current === nextDropIntent) {
      return;
    }

    dropIntentRef.current = nextDropIntent;
    setDropIntent(nextDropIntent);
  };

  const applyDrop = (
    event: DragEvent<HTMLDivElement>,
    nextDropIntent: 'before' | 'inside' | 'after',
  ) => {
    event.preventDefault();
    event.stopPropagation();
    const source = readInternalDragPayload() ?? readDragPayload(event, null);
    if (!source) {
      updateDropIntent(null);
      return;
    }
    if (source.id === node.id) {
      updateDropIntent(null);
      return;
    }

    const parentId = nextDropIntent === 'inside' ? node.id : (node.parentId ?? null);
    const targetIndex =
      nextDropIntent === 'inside'
        ? node.children.length
        : getReorderIndex(nextDropIntent, node, source);

    updateDropIntent(null);
    setActiveDragPayload(null);

    void moveNode(source.id, {
      parentId,
      targetIndex,
      categoryId: node.categoryId,
      isBacklog: node.isBacklog,
    }).catch(() => undefined);
  };

  const handleInsideDragOver = (event: DragEvent<HTMLDivElement>) => {
    const source = readInternalDragPayload() ?? readDragPayload(event, null);
    if (!source || source.id === node.id) {
      updateDropIntent(null);
      return;
    }
    handleDragOver(event);
    updateDropIntent('inside');
  };

  const handleReorderDragOver = (
    event: DragEvent<HTMLDivElement>,
    nextDropIntent: 'before' | 'after',
  ) => {
    const source = readInternalDragPayload() ?? readDragPayload(event, null);
    if (!source || source.id === node.id) {
      updateDropIntent(null);
      return;
    }
    handleDragOver(event);
    updateDropIntent(nextDropIntent);
  };

  const handleInsideDrop = (event: DragEvent<HTMLDivElement>) => {
    void applyDrop(event, 'inside');
  };

  const handleReorderDrop = (
    event: DragEvent<HTMLDivElement>,
    nextDropIntent: 'before' | 'after',
  ) => {
    void applyDrop(event, nextDropIntent);
  };

  return (
    <div
      className={`node-card-wrapper${renderChildren ? '' : ' flat-row'}`}
      data-node-id={node.id}
      data-depth={depth}
      data-lane={childVisibility}
      style={
        {
          '--cat-color': accentColor,
          '--node-depth': depth,
        } as CSSProperties
      }
    >
      {!renderChildren && depth > 0 && (
        <span className="node-tree-connectors" aria-hidden="true">
          {Array.from({ length: depth }, (_, level) => (
            <span
              key={level}
              className={
                level === depth - 1
                  ? 'node-tree-connector branch-end'
                  : continuationDepths.includes(level)
                    ? 'node-tree-connector continues'
                    : 'node-tree-connector'
              }
            />
          ))}
        </span>
      )}
      {dragMode === 'native' && (
        <div
          className={`node-drop-lane before${dropIntent === 'before' ? ' active' : ''}`}
          onDragOver={(event) => handleReorderDragOver(event, 'before')}
          onDragEnter={(event) => handleReorderDragOver(event, 'before')}
          onDragLeave={() => {
            if (dropIntentRef.current === 'before') {
              updateDropIntent(null);
            }
          }}
          onDrop={(event) => handleReorderDrop(event, 'before')}
          aria-hidden="true"
        />
      )}
      <div
        ref={cardRef}
        className={[
          'node-card',
          isCompleted ? 'completed' : '',
          node.isBacklog ? 'is-backlog' : '',
          isArchived ? 'is-archived' : '',
          displayDragging ? 'dragging' : '',
          isSelected ? 'selected' : '',
          isCalendarSpotlighted ? 'calendar-focus-spotlight' : '',
          isExecutionSelected ? 'execution-selected' : '',
          hasStagedDescendant ? 'has-staged-descendant' : '',
          displayDropIntent ? `drop-${displayDropIntent}` : '',
        ]
          .filter(Boolean)
          .join(' ')}
        tabIndex={0}
        role="treeitem"
        aria-expanded={hasVisibleChildren ? isExpandedVisible : undefined}
        aria-label={`${node.title}${workspaceLabel ? `, ${workspaceLabel} workspace` : ''}, ${
          isRecurring ? 'Recurring task' : 'Task'
        }, ${statusLabel}${isScheduled ? ', scheduled' : ''}${
          isExecutionSelected
            ? ', selected for execution'
            : hasStagedDescendant
              ? `, ${stagedDescendantCount} staged subtasks`
              : ''
        }`}
        aria-level={depth + 1}
        aria-selected={isSelected}
        onKeyDown={handleKeyDown}
        onClick={(event) => {
          event.stopPropagation();
          onSelect?.(node.id);
        }}
        onContextMenu={(event) => openContextMenu(event)}
        draggable={dragMode === 'native'}
        onDragStart={dragMode === 'native' ? handleDragStart : undefined}
        onDragEnd={dragMode === 'native' ? handleDragEnd : undefined}
        onPointerDown={
          dragMode === 'pointer'
            ? (event) => onPointerDragStart?.(node, event)
            : undefined
        }
      >
        <div
          ref={rowRef}
          className="node-card-row"
          onDragOver={dragMode === 'native' ? handleInsideDragOver : undefined}
          onDragEnter={dragMode === 'native' ? handleInsideDragOver : undefined}
          onDragLeave={dragMode === 'native' ? handleDragLeave : undefined}
          onDrop={dragMode === 'native' ? handleInsideDrop : undefined}
        >
          <div className="node-row-main">
            {hasVisibleChildren ? (
              <button
                type="button"
                className={`node-chevron${isExpandedVisible ? ' expanded' : ''}`}
                onClick={(event) => {
                  event.stopPropagation();
                  if (onToggleExpanded) {
                    onToggleExpanded(node.id);
                    return;
                  }
                  setIsExpanded((value) => !value);
                }}
                aria-label={isExpandedVisible ? 'Collapse children' : 'Expand children'}
                tabIndex={-1}
              >
                <svg
                  className="node-chevron-icon"
                  viewBox="0 0 12 12"
                  aria-hidden="true"
                >
                  <path d="M4 2.5 7.5 6 4 9.5" />
                </svg>
              </button>
            ) : (
              <span className="node-chevron-spacer" aria-hidden="true" />
            )}

            <div className="node-checkbox-wrap">
              <input
                type="checkbox"
                className="node-checkbox"
                checked={isCompleted}
                onChange={(event) => handleCheckboxChange(event.target.checked)}
                aria-label={`Mark "${node.title}" as ${isCompleted ? 'incomplete' : 'complete'}`}
                tabIndex={-1}
              />
            </div>

            <div className="node-identity-wrap">
              <button
                type="button"
                className="node-emoji-badge"
                aria-label={`Pick emoji for ${node.title}`}
                onClick={handleEmojiButtonClick}
              >
                <span>{displayEmoji}</span>
              </button>

              <div className="node-title-cluster">
                <div className="node-title-wrap">
                  <span
                    ref={titleRef}
                    className="node-title"
                    contentEditable={isEditing}
                    suppressContentEditableWarning
                    onClick={handleTitleClick}
                    onDoubleClick={handleTitleDoubleClick}
                    onBlur={isEditing ? commitEdit : undefined}
                    aria-label={`Task title: ${node.title}`}
                  >
                    {node.title}
                  </span>
                </div>

                {isArchived && (
                  <span className="node-state-pill archived" title="Archived historical task">
                    Archived
                  </span>
                )}

                {isScheduled && (
                  <span
                    className="node-state-icon scheduled"
                    title="Scheduled"
                    aria-hidden="true"
                  >
                    <CalendarCheck2 size={13} strokeWidth={2} />
                  </span>
                )}

                {isRecurring && (
                  <span
                    className="node-state-icon recurring"
                    title={`Recurring: ${formatRecurrenceSummary(node.recurrenceRule)}`}
                    aria-hidden="true"
                  >
                    <RefreshCw size={12} strokeWidth={2} />
                  </span>
                )}

                <button
                  type="button"
                  className="node-add-child-btn"
                  onClick={(event) => {
                    event.stopPropagation();
                    setIsExpanded(true);
                    void addNode({
                      title: 'New Subtask',
                      categoryId: node.categoryId,
                      parentId: node.id,
                      nodeType: 'task',
                      priority: node.priority,
                    });
                  }}
                  aria-label={`Add subtask to ${node.title}`}
                  title="Add subtask"
                >
                  +
                </button>
              </div>
            </div>
          </div>

          <div className="node-row-meta">
            <div ref={deadlineControlRef} className="node-meta-control">
              <button
                type="button"
                className={`node-meta-deadline${!deadlineLabel ? ' empty has-icon' : ''}`}
                aria-label={
                  deadlineLabel
                    ? `Deadline: ${deadlineLabel}. Click to edit.`
                    : 'No deadline set. Click to add one.'
                }
                onClick={handleDeadlineEdit}
              >
                {deadlineLabel ?? <CalendarMetaIcon />}
              </button>

              {isDeadlinePickerOpen &&
                deadlinePopoverPosition &&
                createPortal(
                  <div
                    ref={deadlinePopoverRef}
                    className="node-meta-popover"
                    style={deadlinePopoverPosition}
                    onClick={(event) => event.stopPropagation()}
                  >
                    <label className="node-meta-popover-label">
                      Deadline
                      <input
                        ref={deadlineInputRef}
                        type="date"
                        className="node-meta-picker-input"
                        value={node.deadlineAt ?? ''}
                        onChange={handleDeadlineInputChange}
                      />
                    </label>

                    {node.deadlineAt && (
                      <button
                        type="button"
                        className="node-meta-popover-action"
                        onClick={handleDeadlineClear}
                      >
                        Clear
                      </button>
                    )}
                  </div>,
                  document.body,
                )}
            </div>
            <div ref={priorityControlRef} className="node-meta-control priority-control">
              <button
                type="button"
                className={`node-priority-trigger${isPriorityPickerOpen ? ' active' : ''}`}
                aria-label={`Priority: ${priorityLabel}. Click to change.`}
                onClick={handlePriorityEdit}
              >
                <span
                  className={`node-priority-dot tone-${priorityTone(node.priority)}`}
                  aria-hidden="true"
                />
              </button>

              {isPriorityPickerOpen &&
                priorityPopoverPosition &&
                createPortal(
                  <div
                    ref={priorityPopoverRef}
                    className="node-meta-popover priority-popover"
                    style={priorityPopoverPosition}
                    onClick={(event) => event.stopPropagation()}
                  >
                    <div className="node-meta-popover-label">
                      <span>Priority</span>
                    </div>

                    <div className="node-priority-options" role="listbox" aria-label="Priority">
                      {PRIORITY_OPTIONS.map((priority) => (
                        <button
                          key={priority}
                          type="button"
                          className={`node-priority-option${
                            node.priority === priority ? ' selected' : ''
                          }`}
                          onClick={handlePrioritySelect(priority)}
                          role="option"
                          aria-selected={node.priority === priority}
                        >
                          <span
                            className={`node-priority-dot tone-${priorityTone(priority)}`}
                            aria-hidden="true"
                          />
                          <span>{formatPriorityLabel(priority)}</span>
                        </button>
                      ))}
                    </div>
                  </div>,
                  document.body,
                )}
            </div>
            <div ref={estimateControlRef} className="node-meta-control">
              <button
                type="button"
                className={`node-meta-time${!timeLabel ? ' empty has-icon' : ''}`}
                aria-label={timeLabel ? `Time estimate: ${timeLabel}` : 'Add time estimate'}
                onClick={handleTimeEdit}
              >
                {timeLabel ?? <TimerMetaIcon />}
              </button>

              {isEstimatePickerOpen &&
                estimatePopoverPosition &&
                createPortal(
                  <div
                    ref={estimatePopoverRef}
                    className="node-meta-popover estimate-popover"
                    style={estimatePopoverPosition}
                    onClick={(event) => event.stopPropagation()}
                  >
                  <div className="node-meta-popover-label estimate">
                    <span>Estimate</span>
                    {estimateDraftMinutes > 0 && (
                      <strong>{formatEstimateDraft(estimateDraftMinutes)}</strong>
                    )}
                  </div>

                  <div className="node-estimate-wheel-headers" aria-hidden="true">
                    <span>Hours</span>
                    <span>Minutes</span>
                  </div>

                  <div className="node-estimate-wheel-shell" aria-label="Estimate picker">
                    <div className="node-estimate-wheel-highlight" aria-hidden="true" />

                    <div
                      ref={estimateHoursWheelRef}
                      className="node-estimate-wheel-column"
                      onScroll={handleEstimateHoursScroll}
                    >
                      {ESTIMATE_HOUR_OPTIONS.map((hours) => (
                        <button
                          key={hours}
                          type="button"
                          className={`node-estimate-wheel-item${
                            estimateHours === hours ? ' selected' : ''
                          }`}
                          onClick={handleEstimateHoursClick(hours)}
                        >
                          {formatWheelValue(hours)}
                        </button>
                      ))}
                    </div>

                    <div
                      ref={estimateMinutesWheelRef}
                      className="node-estimate-wheel-column"
                      onScroll={handleEstimateMinutesScroll}
                    >
                      {ESTIMATE_MINUTE_OPTIONS.map((minutes) => (
                        <button
                          key={minutes}
                          type="button"
                          className={`node-estimate-wheel-item${
                            estimateMinutes === minutes ? ' selected' : ''
                          }`}
                          onClick={handleEstimateMinutesClick(minutes)}
                        >
                          {formatWheelValue(minutes)}
                        </button>
                      ))}
                    </div>
                  </div>

                  <div className="node-meta-popover-actions">
                    <button
                      type="button"
                      className="node-meta-popover-action"
                      onClick={handleEstimateSave}
                    >
                      Save
                    </button>
                    <button
                      type="button"
                      className="node-meta-popover-action subtle"
                      onClick={handleEstimateClear}
                    >
                      Clear
                    </button>
                  </div>
                  </div>,
                  document.body,
                )}
            </div>

            <div className="node-meta-control stage-control">
              <button
                type="button"
                className={`node-stage-trigger${
                  isExecutionSelected ? ' active' : ''
                }${hasStagedDescendant ? ' has-descendants' : ''}`}
                aria-label={
                  isExecutionSelected
                    ? `Remove ${node.title} from selected tasks`
                    : `Select ${node.title} for execution staging`
                }
                onClick={handleStageToggle}
              >
                <StageMetaIcon active={isExecutionSelected} />
                {!isExecutionSelected && hasStagedDescendant && (
                  <span className="node-stage-count" aria-hidden="true">
                    {stagedDescendantCount}
                  </span>
                )}
              </button>
            </div>

            <div className="node-row-actions">
              <button
                type="button"
                className={`node-icon-btn${isRecurring ? ' active' : ''}`}
                onClick={(event) => {
                  event.stopPropagation();
                  setIsRecurrenceDialogOpen(true);
                }}
                aria-label={isRecurring ? 'Edit recurrence rule' : 'Set recurrence rule'}
                title={isRecurring ? formatRecurrenceSummary(node.recurrenceRule) : 'Set recurrence'}
              >
                <RefreshCw size={15} aria-hidden="true" />
              </button>

              <button
                type="button"
                className="node-icon-btn node-backlog-btn"
                onClick={(event) => {
                  event.stopPropagation();
                  void updateNode(node.id, { isBacklog: !node.isBacklog });
                }}
                aria-label={node.isBacklog ? 'Move node to active tree' : 'Move node to backlog'}
                title={node.isBacklog ? 'Move to active tree' : 'Move to backlog'}
              >
                ↩
              </button>

              <button
                type="button"
                className="node-icon-btn delete"
                onClick={(event) => {
                  event.stopPropagation();
                  setIsDeleteConfirmOpen(true);
                }}
                aria-label="Delete node"
                title="Delete node"
              >
                ✕
              </button>
            </div>
          </div>
        </div>
        {dragMode === 'native' && (
          <div
            className={`node-drop-lane after-row${dropIntent === 'after' ? ' active' : ''}`}
            onDragOver={(event) => handleReorderDragOver(event, 'after')}
            onDragEnter={(event) => handleReorderDragOver(event, 'after')}
            onDragLeave={() => {
              if (dropIntentRef.current === 'after') {
                updateDropIntent(null);
              }
            }}
            onDrop={(event) => handleReorderDrop(event, 'after')}
            aria-hidden="true"
          />
        )}

        {isEmojiPickerOpen && (
          <div
            ref={pickerRef}
            className="node-emoji-picker-popover"
            onClick={(event) => event.stopPropagation()}
          >
            <Suspense fallback={<span aria-live="polite">Loading emoji picker…</span>}>
              <EmojiPicker
                autoFocus
                hideBorder
                frequentlyUsedEmojis={['📝', '🧠', '🔎', '🎨', '💼', '📈', '🔥', '🔁']}
                customEmojiKeywords={{
                  [displayEmoji]: [node.title.toLowerCase()],
                }}
                customKeywordMostRelevantEmoji={{
                  [node.title.toLowerCase()]: displayEmoji,
                }}
                onEmojiClick={(emojiVariant, resetEmojiPickerState) => {
                  void updateNode(node.id, { emoji: emojiVariant });
                  resetEmojiPickerState();
                  setIsEmojiPickerOpen(false);
                }}
                onBlur={(resetEmojiPickerState) => {
                  resetEmojiPickerState();
                  setIsEmojiPickerOpen(false);
                }}
                onEscapeKeyDown={() => setIsEmojiPickerOpen(false)}
              />
            </Suspense>
          </div>
        )}

        {contextMenuPosition && createPortal(
          <div
            ref={contextMenuRef}
            className="node-context-menu"
            style={{
              left: `${contextMenuPosition.x}px`,
              top: `${contextMenuPosition.y}px`,
              '--cat-color': accentColor,
            } as CSSProperties}
            onClick={(event) => event.stopPropagation()}
            role="menu"
            aria-label={`Task actions for ${node.title}`}
          >
            <button
              type="button"
              className="node-context-menu-item"
              onClick={handleContextMenuAction('stage')}
              role="menuitem"
            >
              <ListChecks size={16} aria-hidden="true" />
              {isExecutionSelected ? 'Remove from selected' : 'Select for execution'}
            </button>
            <button
              type="button"
              className="node-context-menu-item"
              onClick={handleContextMenuAction('recurring')}
              role="menuitem"
            >
              <RefreshCw size={16} aria-hidden="true" />
              {isRecurring ? 'Edit recurrence' : 'Mark as recurring'}
            </button>
            <button
              type="button"
              className="node-context-menu-item"
              onClick={handleContextMenuAction('backlog')}
              role="menuitem"
            >
              {node.isBacklog
                ? <ArchiveRestore size={16} aria-hidden="true" />
                : <Archive size={16} aria-hidden="true" />}
              {node.isBacklog ? 'Move to active tree' : 'Move to backlog'}
            </button>
            <button
              type="button"
              className="node-context-menu-item danger"
              onClick={handleContextMenuAction('delete')}
              role="menuitem"
            >
              <Trash2 size={16} aria-hidden="true" />
              Delete task{descendantCount > 0 ? ` and ${descendantCount} subtask${descendantCount === 1 ? '' : 's'}` : ''}
            </button>
          </div>,
          document.body,
        )}

        {isDeleteConfirmOpen && createPortal(
          <div
            className="node-delete-dialog-backdrop"
            role="presentation"
            onMouseDown={(event) => {
              if (event.target === event.currentTarget && !isDeleting) {
                setIsDeleteConfirmOpen(false);
              }
            }}
          >
            <section
              className="node-delete-dialog"
              role="dialog"
              aria-modal="true"
              aria-labelledby={`delete-task-title-${node.id}`}
              aria-describedby={`delete-task-description-${node.id}`}
            >
              <h2 id={`delete-task-title-${node.id}`}>
                <span className="node-delete-dialog-icon" aria-hidden="true">
                  <TriangleAlert size={20} />
                </span>
                <span>Delete “{node.title}”?</span>
              </h2>
              <p id={`delete-task-description-${node.id}`}>
                {descendantCount > 0
                  ? `This permanently deletes this task and all ${descendantCount} of its subtasks and children.`
                  : 'This permanently deletes this task.'}
              </p>
              <div className="node-delete-dialog-actions">
                <button
                  type="button"
                  autoFocus
                  onClick={() => setIsDeleteConfirmOpen(false)}
                  disabled={isDeleting}
                >
                  <X size={16} aria-hidden="true" />
                  Cancel
                </button>
                <button
                  type="button"
                  className="danger"
                  onClick={() => void confirmDelete()}
                  disabled={isDeleting}
                >
                  <Trash2 size={16} aria-hidden="true" />
                  {isDeleting
                    ? 'Deleting…'
                    : descendantCount > 0
                      ? `Delete task and ${descendantCount} subtask${descendantCount === 1 ? '' : 's'}`
                      : 'Delete task'}
                </button>
              </div>
            </section>
          </div>,
          document.body,
        )}

        {isRecurrenceDialogOpen && createPortal(
          <Suspense fallback={null}>
            <RecurrenceRuleDialog
              node={node}
              onClose={() => setIsRecurrenceDialogOpen(false)}
            />
          </Suspense>,
          document.body,
        )}

        {renderChildren && isExpandedVisible && hasVisibleChildren && (
          <div className="node-children" role="group" aria-label={`Subtasks of ${node.title}`}>
            {visibleChildren.map((child) => (
              <NodeCard
                key={child.id}
                node={child}
                accentColor={accentColor}
                depth={depth + 1}
                childVisibility={childVisibility}
                onStatusChange={onStatusChange}
                onTitleChange={onTitleChange}
                pendingFocusNodeId={pendingFocusNodeId}
                onAutoFocusHandled={onAutoFocusHandled}
                spotlightNodeId={spotlightNodeId}
                onSelect={onSelect}
                isSelected={selectedNodeId === child.id}
                selectedNodeId={selectedNodeId}
                stagedNodeIdSet={stagedNodeIdSet}
                scheduledNodeIdSet={scheduledNodeIdSet}
                onToggleStage={onToggleStage}
                onOpenBrief={onOpenBrief}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
});

function containsNodeId(node: StrategyNode, id: string): boolean {
  return node.id === id || node.children.some((child) => containsNodeId(child, id));
}

function countDescendants(nodes: StrategyNode[]): number {
  return nodes.reduce(
    (count, child) => count + 1 + countDescendants(child.children),
    0,
  );
}

function countStagedDescendants(
  nodes: StrategyNode[],
  stagedNodeIdSet: Set<string>,
  childVisibility: 'active' | 'backlog' | 'archived',
): number {
  return nodes.reduce((count, node) => {
    const visibleChildren = visibleChildrenForLane(node, childVisibility);

    return (
      count +
      (stagedNodeIdSet.has(node.id) ? 1 : 0) +
      countStagedDescendants(visibleChildren, stagedNodeIdSet, childVisibility)
    );
  }, 0);
}
