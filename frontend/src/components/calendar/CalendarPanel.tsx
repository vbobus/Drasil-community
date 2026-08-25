import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { SharedCalendarCanvas } from './SharedCalendarCanvas';
import { useCalendarStore } from '../../store/useCalendarStore';
import { suggestEmoji } from '../../lib/emoji';
import type { NodeDragPayload, StrategyNode } from '../../types/node';
import type { CalendarBlock, CalendarBlockChild, CalendarViewMode } from '../../types/calendar';

interface CalendarPanelProps {
  accentColor: string;
  headingId: string;
  taskLookup: Map<string, StrategyNode>;
  onTaskStatusChange: (id: string, done: boolean) => void;
  onTaskFocus: (taskId: string, categoryId: number) => void;
  onRegisterPointerDrop: (handler: CalendarPointerDropHandler | null) => void;
}

export type CalendarPointerDropHandler = (
  payload: NodeDragPayload,
  clientX: number,
  clientY: number,
) => boolean;

const DAY_START_HOUR = 0;
const DAY_END_HOUR = 24;
const SNAP_MINUTES = 15;
const FALLBACK_DURATION_MINUTES = 30;

function startOfLocalDay(date: Date) {
  const nextDate = new Date(date);
  nextDate.setHours(0, 0, 0, 0);
  return nextDate;
}

function addMinutes(date: Date, minutes: number) {
  const nextDate = new Date(date);
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

function dateFromLocalKey(key: string) {
  const [year, month, day] = key.split('-').map(Number);
  if (!year || !month || !day) {
    return null;
  }
  const date = new Date(year, month - 1, day);
  date.setHours(0, 0, 0, 0);
  return date;
}

function startOfLocalWeek(date: Date) {
  const start = startOfLocalDay(date);
  start.setDate(start.getDate() - ((start.getDay() + 6) % 7));
  return start;
}

function startOfLocalMonth(date: Date) {
  const start = startOfLocalDay(date);
  start.setDate(1);
  return start;
}

function buildVisibleRange(visibleDate: Date, viewMode: CalendarViewMode) {
  const start =
    viewMode === 'week'
      ? startOfLocalWeek(visibleDate)
      : viewMode === 'month'
        ? startOfLocalWeek(startOfLocalMonth(visibleDate))
        : startOfLocalDay(visibleDate);
  const end = new Date(start);
  if (viewMode === 'week') {
    end.setDate(start.getDate() + 7);
  } else if (viewMode === 'month') {
    end.setDate(start.getDate() + 42);
  } else {
    end.setDate(start.getDate() + 1);
  }
  return {
    startAt: toLocalIsoMinute(start),
    endAt: toLocalIsoMinute(end),
  };
}

function resolveDropStartAt(
  visibleDate: Date,
  viewMode: CalendarViewMode,
  dropZone: HTMLElement,
  clientX: number,
  clientY: number,
) {
  const target = document.elementFromPoint(clientX, clientY);
  const targetElement = target instanceof HTMLElement ? target : null;
  const dateElement = targetElement?.closest<HTMLElement>('[data-calendar-date]');
  const surface =
    targetElement?.closest<HTMLElement>('[data-calendar-time-surface]') ?? dropZone;
  const hourGrid = surface.querySelector<HTMLElement>('.calendar-hour-grid');
  const rect = (hourGrid ?? surface).getBoundingClientRect();
  const date =
    dateElement?.dataset.calendarDate
      ? dateFromLocalKey(dateElement.dataset.calendarDate) ?? visibleDate
      : visibleDate;
  const startAt = startOfLocalDay(date);

  if (viewMode === 'month') {
    startAt.setHours(9, 0, 0, 0);
    return startAt;
  }

  const offsetY = Math.max(0, Math.min(rect.height, clientY - rect.top));
  const totalMinutes = (DAY_END_HOUR - DAY_START_HOUR) * 60;
  const rawMinutes = (offsetY / Math.max(1, rect.height)) * totalMinutes;
  const snappedMinutes = Math.round(rawMinutes / SNAP_MINUTES) * SNAP_MINUTES;
  startAt.setHours(DAY_START_HOUR, 0, 0, 0);
  return addMinutes(startAt, Math.min(totalMinutes - SNAP_MINUTES, snappedMinutes));
}

function isPointerInsideCalendarSurface(dropZone: HTMLElement, clientX: number, clientY: number) {
  const surface =
    dropZone.closest<HTMLElement>('.calendar-shell') ??
    dropZone.closest<HTMLElement>('.workspace-panel-calendar') ??
    dropZone;
  const rect = surface.getBoundingClientRect();
  return clientX >= rect.left && clientX <= rect.right && clientY >= rect.top && clientY <= rect.bottom;
}

function childRowsForBlock(
  block: CalendarBlock,
  taskLookup: Map<string, StrategyNode>,
) {
  const task = taskLookup.get(block.taskId);
  const displayNodeEmoji = (node: StrategyNode) => node.emoji || suggestEmoji(node.title, node.type);
  const toCalendarChild = (child: StrategyNode): CalendarBlockChild => ({
    id: child.id,
    title: child.title,
    emoji: displayNodeEmoji(child),
    status: child.status,
    timeEstimateMinutes: child.timeEstimateMinutes,
    children: child.children.map(toCalendarChild),
  });
  const requestedChildren = block.renderedChildTaskIds
    .map((id) => taskLookup.get(id) ?? null)
    .filter((child): child is StrategyNode => child !== null);
  const children = task?.children.length ? task.children : requestedChildren;

  return children.map(toCalendarChild);
}

function isPreviewProposalBlock(block: CalendarBlock) {
  return block.variant === 'draft' || block.variant === 'proposed';
}

export function CalendarPanel({
  accentColor,
  headingId,
  taskLookup,
  onTaskStatusChange,
  onTaskFocus,
  onRegisterPointerDrop,
}: CalendarPanelProps) {
  const [visibleDate, setVisibleDate] = useState(() => startOfLocalDay(new Date()));
  const dropZoneRef = useRef<HTMLDivElement>(null);
  const blocks = useCalendarStore((state) => state.blocks);
  const draftBlocks = useCalendarStore((state) => state.draftBlocks);
  const viewMode = useCalendarStore((state) => state.viewMode);
  const detailLevel = useCalendarStore((state) => state.detailLevel);
  const isLoading = useCalendarStore((state) => state.isLoading);
  const error = useCalendarStore((state) => state.error);
  const fetchBlocks = useCalendarStore((state) => state.fetchBlocks);
  const createManualBlock = useCalendarStore((state) => state.createManualBlock);
  const updateBlockTime = useCalendarStore((state) => state.updateBlockTime);
  const deleteBlock = useCalendarStore((state) => state.deleteBlock);
  const updateDraftBlockTime = useCalendarStore((state) => state.updateDraftBlockTime);
  const removeDraftBlock = useCalendarStore((state) => state.removeDraftBlock);
  const setViewMode = useCalendarStore((state) => state.setViewMode);
  const setDetailLevel = useCalendarStore((state) => state.setDetailLevel);

  const visibleRange = useMemo(
    () => buildVisibleRange(visibleDate, viewMode),
    [viewMode, visibleDate],
  );
  const enrichedBlocks = useMemo(
    () =>
      [...blocks, ...draftBlocks].map((block) => {
        const task = taskLookup.get(block.taskId);
        return {
          ...block,
          emoji: task
            ? task.emoji || suggestEmoji(task.title, task.type)
            : block.emoji || suggestEmoji(block.title, 'task'),
          taskStatus: task?.status ?? block.taskStatus,
          taskDeadlineAt: task?.deadlineAt ?? block.taskDeadlineAt,
          taskPriority: task?.priority ?? block.taskPriority,
          timeEstimateMinutes: task?.timeEstimateMinutes ?? block.timeEstimateMinutes,
          children: childRowsForBlock(block, taskLookup),
        };
      }),
    [blocks, draftBlocks, taskLookup],
  );

  useEffect(() => {
    void fetchBlocks(visibleRange.startAt, visibleRange.endAt);
  }, [fetchBlocks, visibleRange.endAt, visibleRange.startAt]);

  const schedulePayloadAt = useCallback(
    (payload: NodeDragPayload, clientX: number, clientY: number) => {
      const dropZone = dropZoneRef.current;
      const task = taskLookup.get(payload.id);
      if (!dropZone || !task) {
        return false;
      }

      const startAt = resolveDropStartAt(visibleDate, viewMode, dropZone, clientX, clientY);
      const durationMinutes = task.timeEstimateMinutes ?? FALLBACK_DURATION_MINUTES;
      const endAt = addMinutes(startAt, durationMinutes);

      void createManualBlock({
        taskId: task.id,
        categoryId: task.categoryId,
        startAt: toLocalIsoMinute(startAt),
        endAt: toLocalIsoMinute(endAt),
      }).catch(() => undefined);
      return true;
    },
    [createManualBlock, taskLookup, viewMode, visibleDate],
  );

  const handleNativeTaskDrop = useCallback(
    (payload: NodeDragPayload, clientX: number, clientY: number) =>
      schedulePayloadAt(payload, clientX, clientY),
    [schedulePayloadAt],
  );

  const handleBlockTimeChange = useCallback(
    (block: CalendarBlock, startAt: string, endAt: string) => {
      if (isPreviewProposalBlock(block)) {
        updateDraftBlockTime(block.id, { startAt, endAt });
        return;
      }
      void updateBlockTime(block.id, { startAt, endAt }).catch(() => undefined);
    },
    [updateBlockTime, updateDraftBlockTime],
  );

  const handleBlockDelete = useCallback(
    (block: CalendarBlock) => {
      if (isPreviewProposalBlock(block)) {
        removeDraftBlock(block.id);
        return;
      }
      void deleteBlock(block.id).catch(() => undefined);
    },
    [deleteBlock, removeDraftBlock],
  );

  const shiftVisibleDate = useCallback(
    (direction: -1 | 1) => {
      setVisibleDate((current) => {
        const nextDate = new Date(current);
        if (viewMode === 'month') {
          nextDate.setMonth(nextDate.getMonth() + direction);
        } else if (viewMode === 'week') {
          nextDate.setDate(nextDate.getDate() + direction * 7);
        } else {
          nextDate.setDate(nextDate.getDate() + direction);
        }
        return startOfLocalDay(nextDate);
      });
    },
    [viewMode],
  );

  const handleToday = useCallback(() => {
    setVisibleDate(startOfLocalDay(new Date()));
  }, []);

  const handlePointerTaskDrop = useCallback<CalendarPointerDropHandler>(
    (payload, clientX, clientY) => {
      const dropZone = dropZoneRef.current;
      if (!dropZone) {
        return false;
      }

      const isInside = isPointerInsideCalendarSurface(dropZone, clientX, clientY);
      return isInside ? schedulePayloadAt(payload, clientX, clientY) : false;
    },
    [schedulePayloadAt],
  );

  useEffect(() => {
    onRegisterPointerDrop(handlePointerTaskDrop);
    return () => {
      onRegisterPointerDrop(null);
    };
  }, [handlePointerTaskDrop, onRegisterPointerDrop]);

  return (
    <SharedCalendarCanvas
      accentColor={accentColor}
      headingId={headingId}
      blocks={enrichedBlocks}
      visibleDate={visibleDate}
      viewMode={viewMode}
      detailLevel={detailLevel}
      isLoading={isLoading}
      error={error}
      dayStartHour={DAY_START_HOUR}
      dayEndHour={DAY_END_HOUR}
      dropZoneRef={dropZoneRef}
      onViewModeChange={setViewMode}
      onDetailLevelChange={setDetailLevel}
      onNavigatePrevious={() => shiftVisibleDate(-1)}
      onNavigateNext={() => shiftVisibleDate(1)}
      onToday={handleToday}
      onTaskDrop={handleNativeTaskDrop}
      onBlockTimeChange={handleBlockTimeChange}
      onBlockDelete={handleBlockDelete}
      onTaskStatusChange={onTaskStatusChange}
      onTaskFocus={onTaskFocus}
    />
  );
}
