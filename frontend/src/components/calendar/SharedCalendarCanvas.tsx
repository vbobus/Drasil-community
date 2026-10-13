import { useEffect, useMemo, useRef, useState } from 'react';
import type {
  CSSProperties,
  DragEvent,
  FormEvent,
  KeyboardEvent as ReactKeyboardEvent,
  PointerEvent as ReactPointerEvent,
  ReactNode,
  RefObject,
} from 'react';
import type { CalendarBlock, CalendarBlockChild, CalendarViewMode } from '../../types/calendar';
import { DayTaskHolder } from './DayTaskHolder';
import { categoryAccent, categoryFill } from '../../lib/categoryPalette';
import type { StrategyNode, NodeDragPayload } from '../../types/node';
import { CalendarDays, ChevronLeft, ChevronRight, GripVertical, MoveVertical, SlidersHorizontal } from 'lucide-react';
import { calendarRolloverState } from '../../lib/calendarRollover';
import { useMediaQuery } from '../../hooks/useMediaQuery';
import './SharedCalendarCanvas.css';

interface SharedCalendarCanvasProps {
  accentColor: string;
  headingId: string;
  blocks: CalendarBlock[];
  dayTasks?: StrategyNode[];
  onAddTask?: (day: string) => void;
  onAssignDay?: (id: string, day: string) => void;
  visibleDate: Date;
  onDateSelect?: (date: Date) => void;
  viewMode: CalendarViewMode;
  detailLevel: number;
  isLoading: boolean;
  error: string | null;
  dayStartHour: number;
  dayEndHour: number;
  dropZoneRef: RefObject<HTMLDivElement | null>;
  onViewModeChange: (viewMode: CalendarViewMode) => void;
  onDetailLevelChange: (detailLevel: number) => void;
  onNavigatePrevious: () => void;
  onNavigateNext: () => void;
  onToday: () => void;
  onTaskDrop: (payload: NodeDragPayload, clientX: number, clientY: number) => boolean;
  onBlockTimeChange: (block: CalendarBlock, startAt: string, endAt: string) => void;
  onBlockDelete: (block: CalendarBlock) => void;
  onBlockUnschedule?: (block: CalendarBlock, day: string) => void;
  onTaskStatusChange: (id: string, done: boolean) => void;
  onTaskFocus: (taskId: string, categoryId: number) => void;
}

interface PositionedBlock {
  segment: CalendarBlockSegment;
  topPercent: number;
  heightPercent: number;
  heightMinutes: number;
  lane: number;
}

interface CalendarBlockSegment {
  block: CalendarBlock;
  date: Date;
  dateKey: string;
  startAt: Date;
  endAt: Date;
  isFirstSegment: boolean;
  isLastSegment: boolean;
}

type CalendarBlockInteraction = 'move' | 'resize-start' | 'resize-end';

interface InteractionPreview {
  blockId: string;
  startAt: string;
  endAt: string;
  interaction: CalendarBlockInteraction;
}

interface GridPointerPosition {
  date: Date;
  minutes: number;
}

const VIEW_MODES: CalendarViewMode[] = ['day', 'week', 'month'];
const SNAP_MINUTES = 15;
const MIN_BLOCK_MINUTES = 15;
const COMPACT_BLOCK_THRESHOLD_MINUTES = 30;
const COMPACT_BLOCK_MIN_HEIGHT_PX = 14;
const POINTER_DRAG_THRESHOLD = 3;

function EditIcon() {
  return (
    <svg viewBox="0 0 20 20" aria-hidden="true" focusable="false">
      <path d="M4 14.6V17h2.4L15 8.4 12.6 6 4 14.6Z" />
      <path d="m13.4 5.2.9-.9a1.4 1.4 0 0 1 2 2l-.9.9-2-2Z" />
    </svg>
  );
}

function DeleteIcon() {
  return (
    <svg viewBox="0 0 20 20" aria-hidden="true" focusable="false">
      <path d="M4.5 6h11" />
      <path d="M8 6V4.5h4V6" />
      <path d="M6.2 6.8 7 16h6l.8-9.2" />
      <path d="M8.7 9v4.7" />
      <path d="M11.3 9v4.7" />
    </svg>
  );
}

function MoreIcon() {
  return (
    <svg viewBox="0 0 20 20" aria-hidden="true" focusable="false">
      <circle cx="10" cy="4.8" r="1.2" />
      <circle cx="10" cy="10" r="1.2" />
      <circle cx="10" cy="15.2" r="1.2" />
    </svg>
  );
}

function CloseIcon() {
  return (
    <svg viewBox="0 0 20 20" aria-hidden="true" focusable="false">
      <path d="m5.2 5.2 9.6 9.6" />
      <path d="m14.8 5.2-9.6 9.6" />
    </svg>
  );
}

function parseDragPayload(event: DragEvent<HTMLElement>): NodeDragPayload | null {
  const customPayload = event.dataTransfer.getData('application/x-drasil-node');
  const textPayload = event.dataTransfer.getData('text/plain');
  const rawPayload = customPayload || textPayload;

  if (!rawPayload) {
    return null;
  }

  try {
    const parsed = JSON.parse(rawPayload) as Partial<NodeDragPayload>;
    if (
      typeof parsed.id !== 'string' ||
      typeof parsed.categoryId !== 'number' ||
      typeof parsed.sortOrder !== 'number'
    ) {
      return null;
    }
    return {
      id: parsed.id,
      parentId: parsed.parentId ?? null,
      categoryId: parsed.categoryId,
      sortOrder: parsed.sortOrder,
    };
  } catch {
    return null;
  }
}

function hasTaskDragType(event: DragEvent<HTMLElement>) {
  return (
    event.dataTransfer.types.includes('application/x-drasil-node') ||
    event.dataTransfer.types.includes('text/plain')
  );
}

function formatDateTitle(date: Date) {
  return new Intl.DateTimeFormat('en-US', {
    weekday: 'short',
    month: 'long',
    day: 'numeric',
  }).format(date);
}

function formatMonthTitle(date: Date) {
  return new Intl.DateTimeFormat('en-US', {
    month: 'long',
    year: 'numeric',
  }).format(date);
}

function formatWeekTitle(date: Date) {
  const start = startOfLocalWeek(date);
  const end = addDays(start, 6);
  const sameMonth = start.getMonth() === end.getMonth();
  const startLabel = new Intl.DateTimeFormat('en-US', {
    month: 'short',
    day: 'numeric',
  }).format(start);
  const endDay = new Intl.DateTimeFormat('en-US', { day: 'numeric' }).format(end);
  const endLabel = sameMonth
    ? `${endDay}, ${end.getFullYear()}`
    : new Intl.DateTimeFormat('en-US', {
        month: 'short',
        day: 'numeric',
        year: 'numeric',
      }).format(end);
  return `${startLabel} - ${endLabel}`;
}

function formatVisibleTitle(viewMode: CalendarViewMode, date: Date) {
  if (viewMode === 'month') return formatMonthTitle(date);
  if (viewMode === 'week') return formatWeekTitle(date);
  return formatDateTitle(date);
}

function formatTimeLabel(value: Date) {
  return new Intl.DateTimeFormat('en-US', {
    hour: 'numeric',
    minute: '2-digit',
  }).format(value);
}

function formatDetailDate(value: Date) {
  return new Intl.DateTimeFormat('en-US', {
    weekday: 'long',
    month: 'long',
    day: 'numeric',
  }).format(value);
}

function formatDateTimeInput(value: Date) {
  const year = value.getFullYear();
  const month = String(value.getMonth() + 1).padStart(2, '0');
  const day = String(value.getDate()).padStart(2, '0');
  const hours = value.getHours().toString().padStart(2, '0');
  const minutes = value.getMinutes().toString().padStart(2, '0');
  return `${year}-${month}-${day}T${hours}:${minutes}`;
}

function addMinutes(value: Date, minutes: number) {
  const nextDate = new Date(value);
  nextDate.setMinutes(nextDate.getMinutes() + minutes);
  return nextDate;
}

function toLocalIsoMinute(date: Date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  const hours = String(date.getHours()).padStart(2, '0');
  const minutes = String(date.getMinutes()).padStart(2, '0');
  return `${year}-${month}-${day}T${hours}:${minutes}:00`;
}

function startOfLocalDay(date: Date) {
  const nextDate = new Date(date);
  nextDate.setHours(0, 0, 0, 0);
  return nextDate;
}

function startOfLocalWeek(date: Date) {
  const nextDate = startOfLocalDay(date);
  nextDate.setDate(nextDate.getDate() - ((nextDate.getDay() + 6) % 7));
  return nextDate;
}

function addDays(date: Date, days: number) {
  const nextDate = new Date(date);
  nextDate.setDate(nextDate.getDate() + days);
  return nextDate;
}

function localDateKey(date: Date) {
  return [
    date.getFullYear(),
    String(date.getMonth() + 1).padStart(2, '0'),
    String(date.getDate()).padStart(2, '0'),
  ].join('-');
}

function dateFromLocalKey(key: string) {
  const [year, month, day] = key.split('-').map(Number);
  if (!year || !month || !day) {
    return null;
  }
  const date = new Date(year, month - 1, day);
  date.setHours(0, 0, 0, 0);
  return date;
}

function isSameLocalDay(first: Date, second: Date) {
  return localDateKey(first) === localDateKey(second);
}

function segmentsForDay(blocks: CalendarBlock[], date: Date): CalendarBlockSegment[] {
  const dayStart = startOfLocalDay(date);
  const dayEnd = addDays(dayStart, 1);

  return blocks.flatMap((block) => {
    const blockStart = new Date(block.startAt);
    const blockEnd = new Date(block.endAt);
    if (
      !Number.isFinite(blockStart.getTime()) ||
      !Number.isFinite(blockEnd.getTime()) ||
      blockStart >= blockEnd ||
      blockStart >= dayEnd ||
      blockEnd <= dayStart
    ) {
      return [];
    }

    return [{
      block,
      date: dayStart,
      dateKey: localDateKey(dayStart),
      startAt: blockStart > dayStart ? blockStart : dayStart,
      endAt: blockEnd < dayEnd ? blockEnd : dayEnd,
      isFirstSegment: blockStart >= dayStart,
      isLastSegment: blockEnd <= dayEnd,
    }];
  });
}

function buildWeekDays(visibleDate: Date) {
  const weekStart = startOfLocalWeek(visibleDate);
  return Array.from({ length: 7 }, (_, index) => addDays(weekStart, index));
}

function buildMonthDays(visibleDate: Date) {
  const monthStart = startOfLocalDay(visibleDate);
  monthStart.setDate(1);
  const gridStart = startOfLocalWeek(monthStart);
  return Array.from({ length: 42 }, (_, index) => addDays(gridStart, index));
}

function formatGridDayLabel(date: Date, compact = false) {
  return new Intl.DateTimeFormat('en-US', {
    weekday: compact ? undefined : 'short',
    month: compact ? undefined : 'short',
    day: 'numeric',
  }).format(date);
}

function formatAxisWeekday(date: Date) {
  return new Intl.DateTimeFormat('en-US', { weekday: 'short' }).format(date);
}

function formatGmtOffset(date: Date) {
  const offsetMinutes = -date.getTimezoneOffset();
  const sign = offsetMinutes >= 0 ? '+' : '-';
  const absoluteMinutes = Math.abs(offsetMinutes);
  const hours = Math.floor(absoluteMinutes / 60);
  const minutes = absoluteMinutes % 60;
  return minutes === 0
    ? `GMT${sign}${hours}`
    : `GMT${sign}${hours}:${String(minutes).padStart(2, '0')}`;
}

function visibleChildLimitForDetail(detailLevel: number) {
  if (detailLevel < 0.25) return 0;
  if (detailLevel < 0.55) return 2;
  if (detailLevel < 0.82) return 3;
  return 999;
}

function formatHourLabel(hour: number) {
  const date = new Date();
  date.setHours(hour, 0, 0, 0);
  return new Intl.DateTimeFormat('en-US', {
    hour: 'numeric',
  }).format(date);
}

function blockDurationMinutes(block: CalendarBlock) {
  const start = Date.parse(block.startAt);
  const end = Date.parse(block.endAt);
  return Math.max(15, Math.round((end - start) / 60000));
}

function minuteOfDay(date: Date) {
  return date.getHours() * 60 + date.getMinutes();
}

function positionedBlocks(
  segments: CalendarBlockSegment[],
  dayStartHour: number,
  dayEndHour: number,
  rowHeight: number,
): PositionedBlock[] {
  const totalMinutes = (dayEndHour - dayStartHour) * 60;
  const activeLanes: { lane: number; endMinute: number }[] = [];

  return [...segments]
    .sort((first, second) => first.startAt.getTime() - second.startAt.getTime())
    .map((segment) => {
      const startMinute = Math.max(
        0,
        segment.startAt <= segment.date
          ? -dayStartHour * 60
          : minuteOfDay(segment.startAt) - dayStartHour * 60,
      );
      const segmentDayEnd = addDays(segment.date, 1);
      const endMinute = Math.min(
        totalMinutes,
        segment.endAt >= segmentDayEnd
          ? 24 * 60 - dayStartHour * 60
          : minuteOfDay(segment.endAt) - dayStartHour * 60,
      );
      const heightMinutes = Math.max(0, endMinute - startMinute);
      const minimumVisibleMinutes = (COMPACT_BLOCK_MIN_HEIGHT_PX / rowHeight) * 60;
      const laneEndMinute =
        heightMinutes < COMPACT_BLOCK_THRESHOLD_MINUTES
          ? Math.max(endMinute, startMinute + minimumVisibleMinutes)
          : endMinute;
      for (let index = activeLanes.length - 1; index >= 0; index -= 1) {
        if (activeLanes[index].endMinute <= startMinute) {
          activeLanes.splice(index, 1);
        }
      }
      const usedLanes = new Set(activeLanes.map((lane) => lane.lane));
      let lane = 0;
      while (usedLanes.has(lane)) {
        lane += 1;
      }
      activeLanes.push({ lane, endMinute: laneEndMinute });

      return {
        segment,
        topPercent: (startMinute / totalMinutes) * 100,
        heightPercent: (heightMinutes / totalMinutes) * 100,
        heightMinutes,
        lane,
      };
    });
}

function variantLabel(block: CalendarBlock) {
  if (block.variant === 'protected' || block.isProtected) return 'protected';
  if (block.variant === 'draft') return 'draft';
  if (block.variant === 'proposed') return 'proposed';
  return 'scheduled';
}

function hierarchySummary(block: CalendarBlock) {
  const countChildren = (children: CalendarBlock['children'] = []): number =>
    children.reduce((total, child) => total + 1 + countChildren(child.children), 0);
  const childCount = countChildren(block.children);
  if (childCount === 0) {
    return 'no subtasks';
  }
  return `${childCount} ${childCount === 1 ? 'subtask' : 'subtasks'}`;
}

function collectCalendarChildIds(children: CalendarBlock['children'] = []): string[] {
  return children.flatMap((child) => [child.id, ...collectCalendarChildIds(child.children)]);
}

function blockAccessibleName(
  segment: CalendarBlockSegment,
  referenceTime: Date,
  resolvedTaskStatus?: string | null,
) {
  const { block } = segment;
  const canonicalStart = new Date(block.startAt);
  const canonicalEnd = new Date(block.endAt);
  const continuation = [
    !segment.isFirstSegment ? 'continues from previous day' : '',
    !segment.isLastSegment ? 'continues next day' : '',
  ].filter(Boolean).join(' and ');
  return [
    block.title,
    `segment on ${formatDetailDate(segment.date)}`,
    continuation,
    `full interval ${formatDetailDate(canonicalStart)} ${formatTimeLabel(canonicalStart)} to ${formatDetailDate(canonicalEnd)} ${formatTimeLabel(canonicalEnd)}`,
    block.categoryName,
    hierarchySummary(block),
    variantLabel(block),
    calendarRolloverState(block, referenceTime, resolvedTaskStatus)
      ? 'ready to revisit'
      : '',
  ].filter(Boolean).join(', ');
}

function shouldShowChildren(viewMode: CalendarViewMode, detailLevel: number, block: CalendarBlock) {
  return (
    viewMode !== 'month' &&
    visibleChildLimitForDetail(detailLevel) > 0 &&
    (block.children?.length ?? 0) > 0
  );
}

function isTaskDone(status?: string | null) {
  return status === 'done' || status === 'completed';
}

function isDraftProposal(block: CalendarBlock) {
  return block.variant === 'draft' || block.variant === 'proposed';
}

function formatEstimate(minutes?: number | null) {
  if (!minutes) {
    return '';
  }
  if (minutes < 60) {
    return `${minutes}m`;
  }
  const hours = Math.floor(minutes / 60);
  const remainingMinutes = minutes % 60;
  return remainingMinutes > 0 ? `${hours}h ${remainingMinutes}m` : `${hours}h`;
}

function formatDeadlineLabel(deadlineAt?: string | null) {
  if (!deadlineAt) {
    return 'No deadline';
  }

  const dateOnlyMatch = /^(\d{4})-(\d{2})-(\d{2})$/.exec(deadlineAt);
  const parsedDate = dateOnlyMatch
    ? new Date(Number(dateOnlyMatch[1]), Number(dateOnlyMatch[2]) - 1, Number(dateOnlyMatch[3]))
    : new Date(deadlineAt);
  if (Number.isNaN(parsedDate.getTime())) {
    return 'No deadline';
  }

  return new Intl.DateTimeFormat('en-US', {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
  }).format(parsedDate);
}

function formatPriorityLabel(priority?: string | null) {
  if (priority === 'high') return 'High';
  if (priority === 'low') return 'Low';
  return 'Medium';
}

export function SharedCalendarCanvas({
  accentColor,
  headingId,
  blocks,
  dayTasks = [],
  onAddTask,
  onAssignDay,
  visibleDate,
  onDateSelect,
  viewMode,
  detailLevel,
  isLoading,
  error,
  dayStartHour,
  dayEndHour,
  dropZoneRef,
  onViewModeChange,
  onDetailLevelChange,
  onNavigatePrevious,
  onNavigateNext,
  onToday,
  onTaskDrop,
  onBlockTimeChange,
  onBlockDelete,
  onBlockUnschedule,
  onTaskStatusChange,
  onTaskFocus,
}: SharedCalendarCanvasProps) {
  const compact = useMediaQuery('(max-width: 1023px)');
  const hours = Array.from({ length: dayEndHour - dayStartHour + 1 }, (_, index) =>
    dayStartHour + index
  );
  const rowCount = dayEndHour - dayStartHour;
  const rowHeight = 24 + detailLevel * 24;
  const [activeBlockId, setActiveBlockId] = useState<string | null>(null);
  const [isEditingBlock, setIsEditingBlock] = useState(false);
  const [interactionPreview, setInteractionPreview] = useState<InteractionPreview | null>(null);
  const [untimedDropDay, setUntimedDropDay] = useState<string | null>(null);
  const [optimisticTaskSnapshot, setOptimisticTaskSnapshot] = useState<{
    sourceBlocks: CalendarBlock[] | null;
    statuses: Record<string, string>;
  }>({
    sourceBlocks: null,
    statuses: {},
  });
  const optimisticTaskStatuses =
    optimisticTaskSnapshot.sourceBlocks === blocks
      ? optimisticTaskSnapshot.statuses
      : {};
  const activeBlock = useMemo(
    () => blocks.find((block) => block.id === activeBlockId) ?? null,
    [activeBlockId, blocks],
  );
  const activeBlockAllowsCompletion = activeBlock ? !isDraftProposal(activeBlock) : false;
  const [editStartAt, setEditStartAt] = useState('');
  const [editEndAt, setEditEndAt] = useState('');
  const suppressClickBlockIdRef = useRef<string | null>(null);
  const popoverRef = useRef<HTMLDivElement>(null);
  const calendarShellRef = useRef<HTMLDivElement>(null);
  const agendaRef = useRef<HTMLDivElement>(null);
  const dateAxisRef = useRef<HTMLDivElement>(null);
  const pointerCleanupRef = useRef<(() => void) | null>(null);
  useEffect(() => () => pointerCleanupRef.current?.(), []);
  useEffect(() => {
    const agenda = agendaRef.current;
    const axis = dateAxisRef.current;
    if (!agenda) return;
    const measure = () => agenda.style.setProperty('--calendar-sticky-header-height', `${Math.ceil(axis?.getBoundingClientRect().height ?? 0)}px`);
    measure();
    const observer = new ResizeObserver(measure);
    if (axis) observer.observe(axis);
    return () => { observer.disconnect(); agenda.style.removeProperty('--calendar-sticky-header-height'); };
  }, [viewMode]);
  const now = new Date();
  const weekDays = useMemo(() => buildWeekDays(visibleDate), [visibleDate]);
  const monthDays = useMemo(() => buildMonthDays(visibleDate), [visibleDate]);
  const blocksWithPreview = useMemo(
    () =>
      interactionPreview
        ? blocks.map((block) =>
            block.id === interactionPreview.blockId
              ? {
                  ...block,
                  startAt: interactionPreview.startAt,
                  endAt: interactionPreview.endAt,
                }
              : block,
          )
        : blocks,
    [blocks, interactionPreview],
  );
  const daySegments = useMemo(
    () => segmentsForDay(blocksWithPreview, visibleDate),
    [blocksWithPreview, visibleDate],
  );
  const visibleSegments = useMemo(() => {
    if (viewMode === 'day') {
      return daySegments;
    }
    return (viewMode === 'week' ? weekDays : monthDays).flatMap((day) =>
      segmentsForDay(blocksWithPreview, day),
    );
  }, [blocksWithPreview, daySegments, monthDays, viewMode, weekDays]);
  const positioned = useMemo(
    () => positionedBlocks(daySegments, dayStartHour, dayEndHour, rowHeight),
    [dayEndHour, daySegments, dayStartHour, rowHeight],
  );
  const hasBlocks = visibleSegments.length > 0;
  const activeBlockSegment = activeBlock
    ? visibleSegments.find(segment => segment.block.id === activeBlock.id)
      ?? segmentsForDay([activeBlock], startOfLocalDay(new Date(activeBlock.startAt)))[0]
    : null;
  const visibleBlockById = useMemo(
    () => new Map(blocks.map((block) => [block.id, block])),
    [blocks],
  );
  const nowTopPercent =
    ((minuteOfDay(now) - dayStartHour * 60) / Math.max(1, rowCount * 60)) * 100;

  useEffect(() => {
    if (!activeBlock) {
      return;
    }

    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target as Node | null;
      if (target && popoverRef.current?.contains(target)) {
        return;
      }
      if (target && calendarShellRef.current?.contains(target)) {
        const element = target instanceof HTMLElement ? target : null;
        if (element?.closest('.calendar-agenda-block, .calendar-grid-block')) {
          return;
        }
      }
      setActiveBlockId(null);
      setIsEditingBlock(false);
    };

    window.addEventListener('pointerdown', handlePointerDown);
    return () => {
      window.removeEventListener('pointerdown', handlePointerDown);
    };
  }, [activeBlock]);

  useEffect(() => {
    if (!activeBlockId) return;
    const trigger = document.activeElement;
    popoverRef.current?.querySelector<HTMLButtonElement>('[aria-label="Close calendar block details"]')?.focus({ preventScroll: true });
    return () => { if (trigger instanceof HTMLElement && trigger.isConnected) trigger.focus({ preventScroll: true }); };
  }, [activeBlockId]);

  useEffect(() => {
    if (viewMode !== 'week') {
      return;
    }

    const frame = window.requestAnimationFrame(() => {
      calendarShellRef.current
        ?.querySelector<HTMLElement>(`[data-calendar-date="${localDateKey(visibleDate)}"]`)
        ?.scrollIntoView({ block: 'nearest', inline: 'center' });
    });

    return () => {
      window.cancelAnimationFrame(frame);
    };
  }, [viewMode, visibleDate]);

  const handleDragOver = (event: DragEvent<HTMLElement>) => {
    if (!hasTaskDragType(event)) {
      return;
    }

    event.preventDefault();
    // Untimed cards move onto the clock; selected-task references allow copying.
    event.dataTransfer.dropEffect = event.dataTransfer.effectAllowed === 'move' ? 'move' : 'copy';
  };

  const handleDrop = (event: DragEvent<HTMLElement>) => {
    const payload = parseDragPayload(event);
    if (!payload) {
      return;
    }

    event.preventDefault();
    event.stopPropagation();
    onTaskDrop(payload, event.clientX, event.clientY);
  };

  const openBlockDetails = (block: CalendarBlock) => {
    // Mobile keeps the calendar visible while inspecting its booking.
    if (!compact) onTaskFocus(block.taskId, block.categoryId);
    setActiveBlockId(block.id);
    setIsEditingBlock(false);
    setEditStartAt(formatDateTimeInput(new Date(block.startAt)));
    setEditEndAt(formatDateTimeInput(new Date(block.endAt)));
  };

  const pointerGridPosition = (
    clientX: number,
    clientY: number,
    fallbackDate: Date,
  ): GridPointerPosition => {
    const dropZone = dropZoneRef.current;
    if (!dropZone) {
      return { date: fallbackDate, minutes: 0 };
    }

    const target = calendarShellRef.current?.ownerDocument.elementFromPoint(clientX, clientY);
    const targetElement = target instanceof HTMLElement ? target : null;
    const dateElement = targetElement?.closest<HTMLElement>('[data-calendar-date]');
    const surface =
      targetElement?.closest<HTMLElement>('[data-calendar-time-surface]') ?? dropZone;
    const hourGrid = surface.querySelector<HTMLElement>('.calendar-hour-grid');
    const rect = (hourGrid ?? surface).getBoundingClientRect();
    const totalMinutes = rowCount * 60;
    const pointerOffsetY = clientY - rect.top;
    const offsetY = viewMode === 'day'
      ? Math.max(-rect.height, Math.min(rect.height * 2, pointerOffsetY))
      : Math.max(0, Math.min(rect.height, pointerOffsetY));
    const rawMinutes = (offsetY / Math.max(1, rect.height)) * totalMinutes;
    let date =
      dateElement?.dataset.calendarDate
        ? dateFromLocalKey(dateElement.dataset.calendarDate) ?? fallbackDate
        : fallbackDate;
    const snappedMinutes = Math.round(rawMinutes / SNAP_MINUTES) * SNAP_MINUTES;

    if (viewMode === 'day') {
      const dayDelta = Math.floor(snappedMinutes / totalMinutes);
      date = addDays(date, dayDelta);
      return {
        date,
        minutes: snappedMinutes - dayDelta * totalMinutes,
      };
    }

    return {
      date,
      minutes: Math.max(
        0,
        Math.min(totalMinutes, snappedMinutes),
      ),
    };
  };

  const dateAtGridMinute = (gridMinute: number, baseDate: Date) => {
    const startAt = startOfLocalDay(baseDate);
    startAt.setHours(dayStartHour, 0, 0, 0);
    return addMinutes(startAt, gridMinute);
  };

  const previewForPointer = (
    segment: CalendarBlockSegment,
    interaction: CalendarBlockInteraction,
    clientX: number,
    clientY: number,
    pointerOffsetMinutes: number,
  ): InteractionPreview => {
    const { block } = segment;
    const pointerPosition = pointerGridPosition(clientX, clientY, segment.date);
    const pointerAt = dateAtGridMinute(pointerPosition.minutes, pointerPosition.date);
    const canonicalStart = new Date(block.startAt);
    const canonicalEnd = new Date(block.endAt);

    if (interaction === 'resize-start') {
      const latestStart = addMinutes(canonicalEnd, -MIN_BLOCK_MINUTES);
      const nextStart = pointerAt < latestStart ? pointerAt : latestStart;
      return {
        blockId: block.id,
        startAt: toLocalIsoMinute(nextStart),
        endAt: toLocalIsoMinute(canonicalEnd),
        interaction,
      };
    }

    if (interaction === 'resize-end') {
      const earliestEnd = addMinutes(canonicalStart, MIN_BLOCK_MINUTES);
      const nextEnd = pointerAt > earliestEnd ? pointerAt : earliestEnd;
      return {
        blockId: block.id,
        startAt: toLocalIsoMinute(canonicalStart),
        endAt: toLocalIsoMinute(nextEnd),
        interaction,
      };
    }

    const targetSegmentStart = dateAtGridMinute(
      Math.max(0, pointerPosition.minutes - pointerOffsetMinutes),
      pointerPosition.date,
    );
    const deltaMinutes = Math.round(
      (targetSegmentStart.getTime() - segment.startAt.getTime()) / 60000,
    );
    return {
      blockId: block.id,
      startAt: toLocalIsoMinute(addMinutes(canonicalStart, deltaMinutes)),
      endAt: toLocalIsoMinute(addMinutes(canonicalEnd, deltaMinutes)),
      interaction,
    };
  };

  const untimedDayAt = (block: CalendarBlock, x: number, y: number) => {
    if (!onBlockUnschedule || !['scheduled', 'protected'].includes(block.variant)) return null;
    const holder = document.elementFromPoint(x, y)?.closest<HTMLElement>('[data-day-holder]');
    return holder && calendarShellRef.current?.contains(holder) ? holder.dataset.dayHolder ?? null : null;
  };

  const beginBlockPointerInteraction = (
    segment: CalendarBlockSegment,
    event: ReactPointerEvent<HTMLElement>,
    interaction: CalendarBlockInteraction,
    fromDetails = false,
  ) => {
    const { block } = segment;
    if (event.button !== 0 || !event.isPrimary) {
      return;
    }

    // A touch swipe on the booking body scrolls. Only explicit handles can drag.
    if (event.pointerType === 'touch' && !(event.target instanceof Element && event.target.closest('[data-calendar-drag-control]'))) return;

    if (
      event.target instanceof HTMLElement &&
      event.target.closest('input, textarea, select, [contenteditable="true"]')
    ) {
      return;
    }

    event.preventDefault();
    event.stopPropagation();
    pointerCleanupRef.current?.();
    if (!fromDetails) setActiveBlockId(null);

    const pointerId = event.pointerId;
    const captureTarget = event.currentTarget;
    const startX = event.clientX;
    const startY = event.clientY;
    const startMinute = segment.startAt <= segment.date
      ? 0
      : minuteOfDay(segment.startAt) - dayStartHour * 60;
    const startPointer = pointerGridPosition(
      event.clientX,
      event.clientY,
      segment.date,
    );
    const pointerOffsetMinutes =
      interaction === 'move' && !fromDetails
        ? Math.max(0, startPointer.minutes - startMinute)
        : 0;
    let didStartDrag = false;
    let frame: number | null = null;
    let latestX = startX;
    let latestY = startY;
    let latestPreview = previewForPointer(
      segment,
      interaction,
      event.clientX,
      event.clientY,
      pointerOffsetMinutes,
    );

    const commitPreview = (preview: InteractionPreview) => {
      const latestBlock = visibleBlockById.get(block.id) ?? block;
      onBlockTimeChange(latestBlock, preview.startAt, preview.endAt);
    };

    const handlePointerMove = (moveEvent: PointerEvent) => {
      if (moveEvent.pointerId !== pointerId) return;
      const distance = Math.hypot(moveEvent.clientX - startX, moveEvent.clientY - startY);
      if (!didStartDrag && distance < (event.pointerType === 'touch' ? 8 : POINTER_DRAG_THRESHOLD)) {
        return;
      }

      moveEvent.preventDefault();
      if (!didStartDrag && fromDetails) {
        if (captureTarget.hasPointerCapture(pointerId)) captureTarget.releasePointerCapture(pointerId);
        setActiveBlockId(null);
      }
      didStartDrag = true;
      latestX = moveEvent.clientX;
      latestY = moveEvent.clientY;
      if (frame !== null) return;
      frame = window.requestAnimationFrame(() => {
        frame = null;
        const day = interaction === 'move' ? untimedDayAt(block, latestX, latestY) : null;
        setUntimedDropDay(day);
        if (day) { setInteractionPreview(null); return; }
        latestPreview = previewForPointer(segment, interaction, latestX, latestY, pointerOffsetMinutes);
        setInteractionPreview(latestPreview);
      });
    };

    const clearPointerListeners = () => {
      window.removeEventListener('pointermove', handlePointerMove);
      window.removeEventListener('pointerup', handlePointerUp);
      window.removeEventListener('pointercancel', handlePointerCancel);
      if (frame !== null) window.cancelAnimationFrame(frame);
      frame = null;
      pointerCleanupRef.current = null;
    };

    const handlePointerUp = (upEvent: PointerEvent) => {
      if (upEvent.pointerId !== pointerId) return;
      clearPointerListeners();
      if (!didStartDrag) {
        setInteractionPreview(null);
        return;
      }

      upEvent.preventDefault();
      const day = interaction === 'move' ? untimedDayAt(block, upEvent.clientX, upEvent.clientY) : null;
      setUntimedDropDay(null);
      latestPreview = previewForPointer(
        segment,
        interaction,
        upEvent.clientX,
        upEvent.clientY,
        pointerOffsetMinutes,
      );
      setInteractionPreview(null);
      suppressClickBlockIdRef.current = block.id;
      window.setTimeout(() => {
        if (suppressClickBlockIdRef.current === block.id) {
          suppressClickBlockIdRef.current = null;
        }
      }, 0);
      if (day) onBlockUnschedule?.(block, day);
      else commitPreview(latestPreview);
    };

    const cancel = () => {
      clearPointerListeners();
      setInteractionPreview(null);
      setUntimedDropDay(null);
    };
    const handlePointerCancel = (cancelEvent: PointerEvent) => {
      if (cancelEvent.pointerId === pointerId) cancel();
    };

    pointerCleanupRef.current = cancel;
    window.addEventListener('pointermove', handlePointerMove, { passive: false });
    window.addEventListener('pointerup', handlePointerUp);
    window.addEventListener('pointercancel', handlePointerCancel);
  };

  const handleEditSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!activeBlock || !editStartAt || !editEndAt) {
      return;
    }

    const startAt = new Date(editStartAt);
    const endAt = new Date(editEndAt);
    if (
      !Number.isFinite(startAt.getTime()) ||
      !Number.isFinite(endAt.getTime()) ||
      endAt <= startAt
    ) {
      return;
    }

    onBlockTimeChange(activeBlock, toLocalIsoMinute(startAt), toLocalIsoMinute(endAt));
    setIsEditingBlock(false);
  };

  const resolveTaskStatus = (id: string, status?: string | null) =>
    optimisticTaskStatuses[id] ?? status;

  const resolveRolloverState = (block: CalendarBlock) =>
    calendarRolloverState(
      block,
      now,
      resolveTaskStatus(block.taskId, block.taskStatus),
    );

  const handleTaskCompletionChange = (
    id: string,
    done: boolean,
    descendantIds: string[] = [],
  ) => {
    const optimisticIds = [id, ...descendantIds];
    setOptimisticTaskSnapshot({
      sourceBlocks: blocks,
      statuses: Object.fromEntries(
        optimisticIds.map((taskId) => [taskId, done ? 'done' : 'todo']),
      ),
    });
    onTaskStatusChange(id, done);
  };

  const renderThreadNode = (
    child: CalendarBlockChild,
    density: 'card' | 'popover',
    allowCompletion = true,
  ): ReactNode => {
    const estimate = formatEstimate(child.timeEstimateMinutes);
    const completionControl = allowCompletion ? (
        <input
          type="checkbox"
          checked={isTaskDone(resolveTaskStatus(child.id, child.status))}
          aria-label={`Mark ${child.title} done`}
          onPointerDown={(event) => event.stopPropagation()}
          onClick={(event) => event.stopPropagation()}
          onChange={(event) =>
            handleTaskCompletionChange(
              child.id,
              event.target.checked,
              collectCalendarChildIds(child.children),
            )
          }
        />
      ) : (
        <span className="calendar-draft-status-dot" aria-hidden="true" />
      );
    const rowBody = (
      <>
        <span className="calendar-thread-elbow" aria-hidden="true" />
        {completionControl}
        <span className="calendar-thread-emoji" aria-hidden="true">
          {child.emoji ?? ''}
        </span>
        <span className="calendar-thread-title">{child.title}</span>
        {density === 'popover' && estimate && (
          <span className="calendar-thread-estimate">{estimate}</span>
        )}
      </>
    );

    return (
      <div key={child.id} className={`calendar-thread-node is-${density}`}>
        {density === 'card' ? (
          <div className="calendar-thread-row">{rowBody}</div>
        ) : (
          <label className="calendar-thread-row">{rowBody}</label>
        )}
        {child.children?.length ? (
          <div className="calendar-thread-children">
            {child.children.map((nestedChild) =>
              renderThreadNode(nestedChild, density, allowCompletion),
            )}
          </div>
        ) : null}
      </div>
    );
  };

  const moveBlockToDate = (segment: CalendarBlockSegment, date: Date) => {
    const localDayOrdinal = (value: Date) =>
      Date.UTC(value.getFullYear(), value.getMonth(), value.getDate()) / 86_400_000;
    const dayDelta = localDayOrdinal(date) - localDayOrdinal(segment.date);
    const shiftLocalDays = (value: Date) => {
      const shifted = new Date(value);
      shifted.setDate(shifted.getDate() + dayDelta);
      return shifted;
    };
    onBlockTimeChange(
      segment.block,
      toLocalIsoMinute(shiftLocalDays(new Date(segment.block.startAt))),
      toLocalIsoMinute(shiftLocalDays(new Date(segment.block.endAt))),
    );
  };

  const beginDateCardPointerInteraction = (
    segment: CalendarBlockSegment,
    event: ReactPointerEvent<HTMLElement>,
    fromDetails = false,
  ) => {
    const { block } = segment;
    if (event.button !== 0 || !event.isPrimary) {
      return;
    }
    if (event.pointerType === 'touch' && !(event.target instanceof Element && event.target.closest('[data-calendar-drag-control]'))) return;

    if (event.pointerType === 'touch') event.preventDefault();
    event.stopPropagation();
    pointerCleanupRef.current?.();
    if (!fromDetails) setActiveBlockId(null);
    const pointerId = event.pointerId;
    const captureTarget = event.currentTarget;
    const startX = event.clientX;
    const startY = event.clientY;
    let didStartDrag = false;
    let frame: number | null = null;
    let latestX = startX;
    let latestY = startY;

    const clearPointerListeners = () => {
      window.removeEventListener('pointermove', handlePointerMove);
      window.removeEventListener('pointerup', handlePointerUp);
      window.removeEventListener('pointercancel', handlePointerCancel);
      if (frame !== null) window.cancelAnimationFrame(frame);
      frame = null;
      pointerCleanupRef.current = null;
    };

    const handlePointerMove = (moveEvent: PointerEvent) => {
      if (moveEvent.pointerId !== pointerId) return;
      const distance = Math.hypot(moveEvent.clientX - startX, moveEvent.clientY - startY);
      if (!didStartDrag && distance < (event.pointerType === 'touch' ? 8 : POINTER_DRAG_THRESHOLD)) {
        return;
      }

      moveEvent.preventDefault();
      if (!didStartDrag && fromDetails) {
        if (captureTarget.hasPointerCapture(pointerId)) captureTarget.releasePointerCapture(pointerId);
        setActiveBlockId(null);
      }
      didStartDrag = true;
      latestX = moveEvent.clientX;
      latestY = moveEvent.clientY;
      if (frame !== null) return;
      frame = window.requestAnimationFrame(() => {
        frame = null;
        setUntimedDropDay(untimedDayAt(block, latestX, latestY));
      });
    };

    const handlePointerUp = (upEvent: PointerEvent) => {
      if (upEvent.pointerId !== pointerId) return;
      clearPointerListeners();
      if (!didStartDrag) {
        return;
      }

      upEvent.preventDefault();
      const untimedDay = untimedDayAt(block, upEvent.clientX, upEvent.clientY);
      setUntimedDropDay(null);
      const target = calendarShellRef.current?.ownerDocument.elementFromPoint(
        upEvent.clientX,
        upEvent.clientY,
      );
      const dateElement =
        target instanceof HTMLElement
          ? target.closest<HTMLElement>('[data-calendar-date]')
          : null;
      const nextDate = dateElement?.dataset.calendarDate
        ? dateFromLocalKey(dateElement.dataset.calendarDate)
        : null;
      if (nextDate) {
        suppressClickBlockIdRef.current = block.id;
        window.setTimeout(() => {
          if (suppressClickBlockIdRef.current === block.id) {
            suppressClickBlockIdRef.current = null;
          }
        }, 0);
        if (untimedDay) onBlockUnschedule?.(block, untimedDay);
        else moveBlockToDate(segment, nextDate);
      }
    };

    const cancel = () => {
      clearPointerListeners();
      setUntimedDropDay(null);
    };
    const handlePointerCancel = (cancelEvent: PointerEvent) => {
      if (cancelEvent.pointerId === pointerId) cancel();
    };

    pointerCleanupRef.current = cancel;
    window.addEventListener('pointermove', handlePointerMove, { passive: false });
    window.addEventListener('pointerup', handlePointerUp);
    window.addEventListener('pointercancel', handlePointerCancel);
  };

  const renderBlockContents = (segment: CalendarBlockSegment) => {
    const { block } = segment;
    const childRows = block.children ?? [];
    const showChildren = shouldShowChildren(viewMode, detailLevel, block);
    const start = formatTimeLabel(segment.startAt);
    const end = segment.endAt >= addDays(segment.date, 1)
      ? '12:00 AM'
      : formatTimeLabel(segment.endAt);
    const segmentMinutes = Math.max(0, (segment.endAt.getTime() - segment.startAt.getTime()) / 60000);
    const blockPixelHeight = (segmentMinutes / 60) * rowHeight;
    const isTinyBlock = blockPixelHeight < 32;
    const isShortBlock = segmentMinutes < COMPACT_BLOCK_THRESHOLD_MINUTES;
    const isCompactBlock = isTinyBlock || segmentMinutes <= COMPACT_BLOCK_THRESHOLD_MINUTES;
    const allowCompletion = !isDraftProposal(block);
    const rolloverState = resolveRolloverState(block);

    return (
      <>
        <span className="calendar-block-identity" aria-hidden="true">
          {block.emoji ?? block.title.slice(0, 1).toUpperCase()}
        </span>
        <div
          className={[
            'calendar-block-main',
            isCompactBlock ? 'is-compact-content' : '',
            isTinyBlock ? 'is-tiny-content' : '',
          ]
            .filter(Boolean)
            .join(' ')}
        >
          <span className="calendar-block-title">{block.title}</span>
          <span className="calendar-block-time">
            <span className="calendar-compact-comma">, </span>
            {isShortBlock ? start : `${start} - ${end}`}
            {rolloverState && (
              <span className="calendar-rollover-label"> · Ready to revisit</span>
            )}
          </span>
          {(!segment.isFirstSegment || !segment.isLastSegment) && (
            <span className="calendar-block-continuation">
              {!segment.isFirstSegment && !segment.isLastSegment
                ? 'Continues through this day'
                : !segment.isFirstSegment
                  ? 'Continues from previous day'
                  : 'Continues next day'}
            </span>
          )}
          {!isCompactBlock && showChildren && (
            <div
              className="calendar-child-stack"
              onWheel={(event) => event.stopPropagation()}
              onPointerDown={(event) => event.stopPropagation()}
            >
              {childRows.map((child) => renderThreadNode(child, 'card', allowCompletion))}
            </div>
          )}
        </div>
        {allowCompletion ? (
          <input
            type="checkbox"
            className="calendar-block-check"
            checked={isTaskDone(resolveTaskStatus(block.taskId, block.taskStatus))}
            aria-label={`Mark ${block.title} done`}
            onClick={(event) => event.stopPropagation()}
            onPointerDown={(event) => event.stopPropagation()}
            onChange={(event) =>
              handleTaskCompletionChange(
                block.taskId,
                event.target.checked,
                collectCalendarChildIds(block.children),
              )
            }
          />
        ) : (
          <span className="calendar-draft-status-dot is-card" aria-hidden="true" />
        )}
      </>
    );
  };

  const blocksForDay = (date: Date) => segmentsForDay(blocks, date);

  const renderedBlocksForDay = (date: Date) => segmentsForDay(blocksWithPreview, date);

  const renderTimedBlock = (
    positionedBlock: PositionedBlock,
    density: 'day' | 'week' = 'day',
  ) => {
    const { segment, topPercent, heightPercent, heightMinutes, lane } = positionedBlock;
    const { block } = segment;
    const isPreviewed = interactionPreview?.blockId === block.id;
    const blockHeightPx = (heightMinutes / 60) * rowHeight;
    const isTinyBlock = blockHeightPx < 32;
    const isShortBlock = heightMinutes < COMPACT_BLOCK_THRESHOLD_MINUTES;
    const rolloverState = resolveRolloverState(block);

    return (
      <div
        key={`${block.variant}-${block.id}-${segment.dateKey}`}
        role="button"
        tabIndex={0}
        className={[
          'calendar-agenda-block',
          density === 'week' ? 'is-week-block' : '',
          !segment.isFirstSegment ? 'continues-before' : '',
          !segment.isLastSegment ? 'continues-after' : '',
          isTinyBlock ? 'is-tiny-block' : '',
          isShortBlock ? 'is-short-block' : '',
          blockHeightPx >= 88 ? 'has-touch-handles' : '',
          `variant-${block.variant}`,
          rolloverState ? 'is-rollover' : '',
          isPreviewed && density === 'day' ? 'is-being-edited' : '',
        ]
          .filter(Boolean)
          .join(' ')}
        data-variant={block.variant}
        data-rollover-state={rolloverState ?? undefined}
        data-calendar-block-id={block.id}
        data-calendar-segment-date={segment.dateKey}
        data-duration-minutes={heightMinutes}
        aria-label={blockAccessibleName(
          segment,
          now,
          resolveTaskStatus(block.taskId, block.taskStatus),
        )}
        style={
          {
            '--block-top': `${topPercent}%`,
            '--block-height': `${heightPercent}%`,
            '--block-height-px': `${blockHeightPx}px`,
            '--block-lane': lane,
            '--block-color': categoryAccent(block.categoryColor),
            '--block-fill': categoryFill(block.categoryColor),
          } as CSSProperties
        }
        onClick={() => {
          if (suppressClickBlockIdRef.current === block.id) {
            return;
          }
          openBlockDetails(block);
        }}
        onKeyDown={(event: ReactKeyboardEvent<HTMLDivElement>) => {
          if (event.target !== event.currentTarget) return;
          if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault();
            openBlockDetails(block);
          }
        }}
        onContextMenu={(event) => {
          event.preventDefault();
          openBlockDetails(block);
        }}
        onPointerDown={(event) => beginBlockPointerInteraction(segment, event, 'move')}
        onDragEnter={handleDragOver}
        onDragOver={handleDragOver}
        onDrop={handleDrop}
      >
        {segment.isFirstSegment && (
          <button
            type="button"
            className="calendar-resize-handle is-start"
            data-calendar-drag-control="resize-start"
            data-testid={`calendar-resize-start-${block.id}`}
            aria-label="Resize calendar block start"
            onPointerDown={(event) => beginBlockPointerInteraction(segment, event, 'resize-start')}
          />
        )}
        {renderBlockContents(segment)}
        {blockHeightPx >= 88 && <button type="button" className="calendar-block-move-handle"
          data-calendar-drag-control="move" aria-label={`Move ${block.title}`} title="Drag to move"
          onClick={event => { event.stopPropagation(); openBlockDetails(block); }}
          onPointerDown={event => beginBlockPointerInteraction(segment, event, 'move')}>
          <GripVertical size={18} aria-hidden="true" />
        </button>}
        {segment.isLastSegment && (
          <button
            type="button"
            className="calendar-resize-handle is-end"
            data-calendar-drag-control="resize-end"
            data-testid={`calendar-resize-end-${block.id}`}
            aria-label="Resize calendar block end"
            onPointerDown={(event) => beginBlockPointerInteraction(segment, event, 'resize-end')}
          />
        )}
      </div>
    );
  };

  const renderGridCard = (segment: CalendarBlockSegment) => {
    const { block } = segment;
    const rolloverState = resolveRolloverState(block);
    const timeCopy = !segment.isFirstSegment
      ? segment.isLastSegment
        ? `Until ${formatTimeLabel(segment.endAt)}`
        : 'Continues'
      : !segment.isLastSegment
        ? `${formatTimeLabel(segment.startAt)}, continues`
        : formatTimeLabel(segment.startAt);
    return (
    <div
      key={`${block.variant}-${block.id}-${segment.dateKey}`}
      role="button"
      tabIndex={0}
      className={`calendar-grid-block variant-${block.variant}${rolloverState ? ' is-rollover' : ''}${!segment.isFirstSegment ? ' continues-before' : ''}${!segment.isLastSegment ? ' continues-after' : ''}`}
      data-variant={block.variant}
      data-rollover-state={rolloverState ?? undefined}
      data-calendar-block-id={block.id}
      data-calendar-segment-date={segment.dateKey}
      aria-label={blockAccessibleName(
        segment,
        now,
        resolveTaskStatus(block.taskId, block.taskStatus),
      )}
      style={
        {
          '--block-color': categoryAccent(block.categoryColor),
            '--block-fill': categoryFill(block.categoryColor),
        } as CSSProperties
      }
      onClick={() => {
        if (suppressClickBlockIdRef.current === block.id) {
          return;
        }
        openBlockDetails(block);
      }}
      onKeyDown={(event: ReactKeyboardEvent<HTMLDivElement>) => {
        if (event.target !== event.currentTarget) return;
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          openBlockDetails(block);
        }
      }}
      onContextMenu={(event) => {
        event.preventDefault();
        openBlockDetails(block);
      }}
      onPointerDown={(event) => beginDateCardPointerInteraction(segment, event)}
      onDragEnter={handleDragOver}
      onDragOver={handleDragOver}
      onDrop={handleDrop}
    >
      <span className="calendar-grid-block-title">{block.title}</span>
      <span className="calendar-grid-block-time">
        {timeCopy}
        {rolloverState && (
          <span className="calendar-rollover-label"> · Ready to revisit</span>
        )}
      </span>
    </div>
    );
  };

  const renderDayTasks = (day: Date) => {
    const key = localDateKey(day);
    return <DayTaskHolder day={key} isDropTarget={key === untimedDropDay} tasks={dayTasks.filter(task => task.plannedDate === key && !blocks.some(block =>
      block.taskId === task.id && block.variant !== 'draft' && block.variant !== 'proposed'
      && new Date(block.startAt) < addDays(startOfLocalDay(day), 1) && new Date(block.endAt) > startOfLocalDay(day)
    ))} onAdd={onAddTask} onAssign={onAssignDay} onFocus={onTaskFocus} onComplete={onTaskStatusChange} />;
  };

  return (
    <div
      ref={calendarShellRef}
      className="calendar-shell"
      style={
        {
          '--calendar-accent': accentColor,
          '--calendar-row-count': rowCount,
          '--calendar-row-height': `${rowHeight}px`,
        } as CSSProperties
      }
    >
      <div className="calendar-topbar">
        <div className="calendar-heading-row">
          <div className="calendar-range-group">
            <CalendarDays className="calendar-heading-icon" size={17} aria-hidden="true" />
            <h2 className="calendar-shell-title" id={headingId}>
              {formatVisibleTitle(viewMode, visibleDate)}
            </h2>
          </div>
          <div className="calendar-navigation-actions">
          <button
            type="button"
            className="calendar-nav-btn"
            aria-label="Previous calendar range"
            onClick={onNavigatePrevious}
          >
            <ChevronLeft size={17} aria-hidden="true" />
          </button>
          <button
            type="button"
            className="calendar-nav-btn"
            aria-label="Next calendar range"
            onClick={onNavigateNext}
          >
            <ChevronRight size={17} aria-hidden="true" />
          </button>
            <button type="button" className="calendar-today-btn" onClick={onToday}>
              Today
            </button>
          </div>
        </div>

        <div className="calendar-view-row">
          <div className="calendar-view-switch" aria-label="Calendar view switcher">
            {VIEW_MODES.map((mode) => (
              <button
                key={mode}
                type="button"
                className={`calendar-view-btn${viewMode === mode ? ' is-active' : ''}`}
                aria-pressed={viewMode === mode}
                onClick={() => onViewModeChange(mode)}
              >
                {mode[0].toUpperCase() + mode.slice(1)}
              </button>
            ))}
          </div>
          <div className="calendar-view-actions">
          <details className="calendar-detail-menu">
            <summary aria-label="Calendar detail" title="Calendar detail"><SlidersHorizontal size={16} aria-hidden="true" /></summary>
            <label className="calendar-detail-control">
              <span>Detail</span>
              <input aria-label="Zoom / Detail" type="range" min="0" max="1" step="0.05" value={detailLevel} onChange={event => onDetailLevelChange(Number(event.target.value))} />
            </label>
          </details>
          <button
            type="button"
            className="calendar-picker-btn"
            aria-label="Jump to today"
            title="Jump to today"
            onClick={onToday}
          >
            <CalendarDays size={16} aria-hidden="true" />
          </button>
          </div>
        </div>
      </div>

      <div
        ref={agendaRef}
        className={[
          'calendar-agenda',
          viewMode === 'week' ? 'is-week-mode' : '',
          viewMode === 'month' ? 'is-summary-mode' : '',
        ]
          .filter(Boolean)
          .join(' ')}
        role="grid"
        aria-label={`Shared calendar ${viewMode} ${viewMode === 'day' ? 'agenda' : 'view'}`}
      >
        {viewMode !== 'month' && (
          <>
            <div className="calendar-axis-corner" aria-hidden="true">
              {formatGmtOffset(visibleDate)}
            </div>
            <div
              ref={dateAxisRef}
              className={`calendar-date-axis${
                viewMode === 'week' ? ' is-week-axis' : ' is-day-axis'
              }`}
              aria-label="Calendar date axis"
            >
              {(viewMode === 'week' ? weekDays : [visibleDate]).map((day) => (
                <div
                  key={localDateKey(day)}
                  className={`calendar-date-axis-cell${
                    isSameLocalDay(day, now) ? ' is-today' : ''
                  }${isSameLocalDay(day, visibleDate) ? ' is-current' : ''}`}
                  data-calendar-date={localDateKey(day)}
                >
                  <button type="button" className="calendar-date-select" aria-label={`Focus ${formatGridDayLabel(day)}`}
                    aria-pressed={isSameLocalDay(day, visibleDate)} disabled={!onDateSelect} onClick={() => onDateSelect?.(day)}>
                    <span>{formatAxisWeekday(day)}</span>
                    <strong>{day.getDate()}</strong>
                  </button>
                  {renderDayTasks(day)}
                </div>
              ))}
            </div>
          </>
        )}

        {viewMode === 'month' && <div ref={dateAxisRef} className="calendar-month-weekdays" aria-label="Days of the week">
          {weekDays.map(day => <span key={day.getDay()}>{formatAxisWeekday(day)}</span>)}
        </div>}

        {viewMode !== 'month' && (
          <div className="calendar-time-rail" aria-hidden="true">
            {hours.slice(0, -1).map((hour) => (
              <span key={hour} className="calendar-time-label">
                {formatHourLabel(hour)}
              </span>
            ))}
          </div>
        )}

        <div
          ref={dropZoneRef}
          className={`calendar-day-track${viewMode !== 'day' ? ' is-summary-view' : ''}`}
          data-testid="calendar-day-drop-zone"
          data-calendar-date={localDateKey(visibleDate)}
          data-calendar-drop-zone="day-track"
          data-calendar-time-surface={viewMode === 'day' ? 'true' : undefined}
          onDragEnter={handleDragOver}
          onDragOver={handleDragOver}
          onDrop={handleDrop}
        >
          {viewMode === 'day' ? (
            <div className="calendar-hour-grid" role="rowgroup">
              {hours.slice(0, -1).map((hour) => (
                <div key={hour} className="calendar-hour-row" role="row">
                  <div
                    className="calendar-hour-cell"
                    role="gridcell"
                    aria-label={formatHourLabel(hour)}
                  />
                </div>
              ))}
            </div>
          ) : viewMode === 'week' ? (
            <div
              className="calendar-week-time-grid"
              data-testid="calendar-week-grid"
              onDragEnter={handleDragOver}
              onDragOver={handleDragOver}
              onDrop={handleDrop}
            >
              {weekDays.map((day) => {
                const dayBlocks = renderedBlocksForDay(day);
                return (
                  <section
                    key={localDateKey(day)}
                    className={`calendar-week-time-column${
                      isSameLocalDay(day, visibleDate) ? ' is-current' : ''
                    }${isSameLocalDay(day, now) ? ' is-today' : ''}`}
                    data-testid="calendar-week-day-cell"
                    data-calendar-date={localDateKey(day)}
                    data-calendar-time-surface="true"
                    aria-label={formatGridDayLabel(day)}
                    onClick={event => {
                      if (event.target instanceof Element && !event.target.closest('button, input, label, a, [data-calendar-block-id]')) onDateSelect?.(day);
                    }}
                  >
                    <div className="calendar-hour-grid" role="rowgroup">
                      {hours.slice(0, -1).map((hour) => (
                        <div key={hour} className="calendar-hour-row" role="row">
                          <div className="calendar-hour-cell" role="gridcell" aria-label={formatHourLabel(hour)} />
                        </div>
                      ))}
                    </div>
                    <div className="calendar-week-block-layer" aria-label={`${formatGridDayLabel(day)} blocks`}>
                      {positionedBlocks(dayBlocks, dayStartHour, dayEndHour, rowHeight).map((positionedBlock) =>
                        renderTimedBlock(positionedBlock, 'week'),
                      )}
                      {isSameLocalDay(day, now) && (
                        <span
                          className="calendar-now-line is-week"
                          style={{ '--now-top': `${nowTopPercent}%` } as CSSProperties}
                          aria-hidden="true"
                        />
                      )}
                    </div>
                  </section>
                );
              })}
            </div>
          ) : (
            <div
              className="calendar-month-grid"
              data-testid="calendar-month-grid"
              onDragEnter={handleDragOver}
              onDragOver={handleDragOver}
              onDrop={handleDrop}
            >
              {monthDays.map((day) => {
                const dayBlocks = blocksForDay(day);
                const isOutsideMonth = day.getMonth() !== visibleDate.getMonth();
                return (
                  <section
                    key={localDateKey(day)}
                    className={[
                      'calendar-month-cell',
                      isSameLocalDay(day, now) ? 'is-today' : '',
                      isSameLocalDay(day, visibleDate) ? 'is-current' : '',
                      isOutsideMonth ? 'is-outside-month' : '',
                      day >= weekDays[0] && day < addDays(weekDays[0], 7) ? 'is-focused-week' : '',
                    ]
                      .filter(Boolean)
                      .join(' ')}
                    data-testid="calendar-month-day-cell"
                    data-calendar-date={localDateKey(day)}
                    data-focused-week={day >= weekDays[0] && day < addDays(weekDays[0], 7) ? 'true' : undefined}
                    aria-label={formatGridDayLabel(day, true)}
                    onClick={event => {
                      if (event.target instanceof Element && !event.target.closest('button, input, label, a, [data-calendar-block-id]')) onDateSelect?.(day);
                    }}
                  >
                    <header><button type="button" className="calendar-date-select" aria-label={`Focus ${formatGridDayLabel(day)}`}
                      aria-pressed={isSameLocalDay(day, visibleDate)} disabled={!onDateSelect} onClick={() => onDateSelect?.(day)}>
                      {formatGridDayLabel(day, true)}
                    </button></header>
                    {renderDayTasks(day)}
                    <div className="calendar-grid-cell-blocks">
                      {dayBlocks.slice(0, 3).map(renderGridCard)}
                      {dayBlocks.length > 3 && (
                        <span className="calendar-month-overflow">
                          +{dayBlocks.length - 3} more
                        </span>
                      )}
                    </div>
                  </section>
                );
              })}
            </div>
          )}
          {isLoading && (
            <div className="calendar-empty-state" role="status">
              Loading calendar...
            </div>
          )}

          {!isLoading && error && (
            <div className="calendar-empty-state" role="alert">
              {error}
            </div>
          )}

          {!isLoading && !error && !hasBlocks && (
            <div className="calendar-empty-state">No scheduled blocks yet</div>
          )}

          {viewMode === 'day' && (
            <div
              className="calendar-block-layer"
              aria-label="Calendar blocks"
              onDragEnter={handleDragOver}
              onDragOver={handleDragOver}
              onDrop={handleDrop}
            >
              {positioned.map((positionedBlock) => renderTimedBlock(positionedBlock))}

              {isSameLocalDay(visibleDate, now) && (
                <span
                  className="calendar-now-line"
                  style={{ '--now-top': `${nowTopPercent}%` } as CSSProperties}
                  aria-hidden="true"
                />
              )}
            </div>
          )}

          {activeBlock && (
            <div
              className="calendar-popover-layer"
              onPointerDown={() => {
                setActiveBlockId(null);
                setIsEditingBlock(false);
              }}
            >
              <div
                ref={popoverRef}
                className="calendar-block-popover"
                role="dialog"
                aria-modal="true"
                aria-label="Calendar block details"
                style={
                  {
                    '--block-color': activeBlock.categoryColor,
                  } as CSSProperties
                }
                onPointerDown={(event) => event.stopPropagation()}
                onKeyDown={(event) => {
                  if (event.key === 'Escape') {
                    event.preventDefault(); event.stopPropagation(); setActiveBlockId(null); setIsEditingBlock(false);
                  } else if (event.key === 'Tab') {
                    const controls = Array.from(event.currentTarget.querySelectorAll<HTMLElement>('button, input, select, textarea, a[href], [tabindex="0"]'))
                      .filter(control => !control.matches(':disabled') && control.getClientRects().length > 0);
                    const first = controls[0]; const last = controls[controls.length - 1];
                    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
                    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
                  }
                }}
              >
                <div className="calendar-popover-heading">
                  <span>Calendar task details</span>
                  <button
                    type="button"
                    aria-label="Close calendar block details"
                    onClick={() => setActiveBlockId(null)}
                  >
                    <CloseIcon />
                  </button>
                </div>

                <div className="calendar-popover-actions">
                  <button
                    type="button"
                    aria-label="Edit calendar block"
                    onClick={() => setIsEditingBlock((isEditing) => !isEditing)}
                  >
                    <EditIcon />
                  </button>
                  <button
                    type="button"
                    aria-label={
                      activeBlock.variant === 'proposed'
                        ? 'Remove proposed calendar block'
                        : activeBlock.variant === 'draft'
                          ? 'Remove draft calendar block'
                        : 'Delete calendar block'
                    }
                    onClick={() => {
                      onBlockDelete(activeBlock);
                      setActiveBlockId(null);
                    }}
                  >
                    <DeleteIcon />
                  </button>
                  <button type="button" aria-label="More calendar block options">
                    <MoreIcon />
                  </button>
                </div>

                {activeBlockSegment && <div className="calendar-touch-drag-tools" aria-label="Touch calendar controls">
                  <p id="calendar-touch-drag-hint">Drag a handle onto the calendar, or tap Edit to enter a time.</p>
                  <div>
                    <button type="button" data-calendar-drag-control="move" aria-label="Move calendar block" aria-describedby="calendar-touch-drag-hint"
                      onClick={() => setIsEditingBlock(true)}
                      onPointerDown={event => viewMode === 'month'
                        ? beginDateCardPointerInteraction(activeBlockSegment, event, true)
                        : beginBlockPointerInteraction(activeBlockSegment, event, 'move', true)}>
                      <GripVertical size={18} aria-hidden="true" /><span>Move</span>
                    </button>
                    {viewMode !== 'month' && <>
                      <button type="button" data-calendar-drag-control="resize-start" aria-label="Resize calendar block start" aria-describedby="calendar-touch-drag-hint"
                        onClick={() => setIsEditingBlock(true)} onPointerDown={event => beginBlockPointerInteraction(activeBlockSegment, event, 'resize-start', true)}>
                        <MoveVertical size={18} aria-hidden="true" /><span>Start</span>
                      </button>
                      <button type="button" data-calendar-drag-control="resize-end" aria-label="Resize calendar block end" aria-describedby="calendar-touch-drag-hint"
                        onClick={() => setIsEditingBlock(true)} onPointerDown={event => beginBlockPointerInteraction(activeBlockSegment, event, 'resize-end', true)}>
                        <MoveVertical size={18} aria-hidden="true" /><span>End</span>
                      </button>
                    </>}
                  </div>
                </div>}

                <div className="calendar-popover-body">
                  <span className="calendar-popover-icon" aria-hidden="true">
                    {activeBlock.emoji ?? activeBlock.title.slice(0, 1).toUpperCase()}
                  </span>
                  <div>
                    <h3>{activeBlock.title}</h3>
                    <p>
                      {formatDetailDate(new Date(activeBlock.startAt))},{' '}
                      {formatTimeLabel(new Date(activeBlock.startAt))} -{' '}
                      {!isSameLocalDay(
                        new Date(activeBlock.startAt),
                        new Date(activeBlock.endAt),
                      ) && `${formatDetailDate(new Date(activeBlock.endAt))}, `}
                      {formatTimeLabel(new Date(activeBlock.endAt))}
                    </p>
                    {activeBlock.isRecurring && <p>Recurring block</p>}
                  </div>
                </div>

                <p className="calendar-popover-subcopy">
                  Full task hierarchy and details for this scheduled block.
                </p>
                {resolveRolloverState(activeBlock) && (
                  <p className="calendar-rollover-callout">
                    This unfinished block is ready to revisit.
                  </p>
                )}

              <div
                className="calendar-popover-hierarchy"
                aria-label="Scheduled task hierarchy"
                onWheel={(event) => event.stopPropagation()}
              >
                <label
                  className="calendar-thread-row is-root"
                >
                  <span className="calendar-thread-elbow" aria-hidden="true" />
                  {activeBlockAllowsCompletion ? (
                    <input
                      type="checkbox"
                      checked={isTaskDone(resolveTaskStatus(activeBlock.taskId, activeBlock.taskStatus))}
                      aria-label={`Mark ${activeBlock.title} done`}
                      onChange={(event) =>
                        handleTaskCompletionChange(
                          activeBlock.taskId,
                          event.target.checked,
                          collectCalendarChildIds(activeBlock.children),
                        )
                      }
                    />
                  ) : (
                    <span className="calendar-draft-status-dot" aria-hidden="true" />
                  )}
                  <span className="calendar-thread-emoji" aria-hidden="true">
                    {activeBlock.emoji ?? ''}
                  </span>
                  <span className="calendar-thread-title">{activeBlock.title}</span>
                  <span className="calendar-thread-estimate">
                    {formatEstimate(blockDurationMinutes(activeBlock))}
                  </span>
                </label>
                {activeBlock.children?.length ? (
                  <div className="calendar-thread-children is-root-children">
                    {activeBlock.children.map((child) =>
                      renderThreadNode(child, 'popover', activeBlockAllowsCompletion),
                    )}
                  </div>
                ) : (
                  <p className="calendar-popover-empty">No subtasks</p>
                )}
              </div>

                <dl className="calendar-popover-facts">
                  <div>
                    <dt>Deadline</dt>
                    <dd>
                      <span className="calendar-fact-icon" aria-hidden="true">D</span>
                      {formatDeadlineLabel(activeBlock.taskDeadlineAt)}
                    </dd>
                  </div>
                  <div>
                    <dt>Priority</dt>
                    <dd>
                      <span
                        className={`calendar-priority-dot priority-${
                          activeBlock.taskPriority ?? 'medium'
                        }`}
                        aria-hidden="true"
                      />
                      {formatPriorityLabel(activeBlock.taskPriority)}
                    </dd>
                  </div>
                  <div>
                    <dt>Estimate</dt>
                    <dd>
                      <span className="calendar-fact-icon" aria-hidden="true">T</span>
                      {formatEstimate(activeBlock.timeEstimateMinutes) ||
                        formatEstimate(blockDurationMinutes(activeBlock))}
                    </dd>
                  </div>
                </dl>

              {isEditingBlock && (
                <form className="calendar-popover-edit" onSubmit={handleEditSubmit}>
                  <label>
                    <span>Start</span>
                    <input
                      aria-label="Calendar block start date and time"
                      type="datetime-local"
                      value={editStartAt}
                      onChange={(event) => setEditStartAt(event.target.value)}
                    />
                  </label>
                  <label>
                    <span>End</span>
                    <input
                      aria-label="Calendar block end date and time"
                      type="datetime-local"
                      value={editEndAt}
                      min={editStartAt}
                      onChange={(event) => setEditEndAt(event.target.value)}
                    />
                  </label>
                  <button type="submit" aria-label="Save calendar block time">
                    Save
                  </button>
                </form>
              )}
              </div>
            </div>
          )}
        </div>
      </div>

    </div>
  );
}
