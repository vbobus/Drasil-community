import type {
  ApiCalendarBlock,
  CalendarBlockTimeUpdate,
  CalendarBlock,
  ManualCalendarBlockCreate,
} from '../types/calendar';
import { apiRequest } from './http';

const BASE = '/api/v1/calendar-blocks';

function normalizeCalendarBlock(block: ApiCalendarBlock): CalendarBlock {
  return {
    id: block.id.toString(),
    taskId: block.taskId.toString(),
    taskStatus: block.taskStatus,
    taskDeadlineAt: block.taskDeadlineAt ?? null,
    taskPriority: block.taskPriority,
    timeEstimateMinutes: block.timeEstimateMinutes ?? null,
    categoryId: block.categoryId,
    title: block.title,
    emoji: block.emoji ?? null,
    categoryName: block.categoryName,
    categoryColor: block.categoryColor,
    startAt: block.startAt,
    endAt: block.endAt,
    variant: block.variant,
    completionStatus: block.completionStatus,
    isPinned: block.isPinned,
    isProtected: block.isProtected,
    isRecurring: block.isRecurring,
    googleEventId: block.googleEventId ?? null,
    googleLastSyncedAt: block.googleLastSyncedAt ?? null,
    googleSyncError: block.googleSyncError ?? null,
    hierarchyDepth: block.hierarchyDepth ?? 0,
    renderedChildTaskIds: (block.renderedChildTaskIds ?? []).map((id) => id.toString()),
  };
}

export async function fetchCalendarBlocks(
  startAt: string,
  endAt: string,
): Promise<CalendarBlock[]> {
  const params = new URLSearchParams({
    start: startAt,
    end: endAt,
  });
  const blocks = await apiRequest<ApiCalendarBlock[]>(`${BASE}?${params.toString()}`);
  return blocks.map(normalizeCalendarBlock);
}

export async function fetchScheduledTaskIds(): Promise<string[]> {
  const taskIds = await apiRequest<number[]>(`${BASE}/scheduled-task-ids`);
  return taskIds.map((taskId) => taskId.toString());
}

export async function createCalendarBlock(
  data: ManualCalendarBlockCreate,
): Promise<CalendarBlock> {
  const block = await apiRequest<ApiCalendarBlock>(BASE, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      taskId: parseInt(data.taskId, 10),
      categoryId: data.categoryId,
      startAt: data.startAt,
      endAt: data.endAt,
    }),
  });
  return normalizeCalendarBlock(block);
}

export async function updateCalendarBlock(
  blockId: string,
  data: CalendarBlockTimeUpdate,
): Promise<CalendarBlock> {
  const block = await apiRequest<ApiCalendarBlock>(`${BASE}/${blockId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      startAt: data.startAt,
      endAt: data.endAt,
    }),
  });
  return normalizeCalendarBlock(block);
}

export async function deleteCalendarBlock(blockId: string): Promise<void> {
  await apiRequest<void>(`${BASE}/${blockId}`, {
    method: 'DELETE',
  });
}
