import type {
  ApiCalendarBlock,
  CalendarBlockTimeUpdate,
  CalendarBlock,
  ManualCalendarBlockCreate,
} from '../types/calendar';
import { apiRequest } from './http';

const BASE = '/api/v1/calendar-blocks';

export function normalizeCalendarBlock(block: ApiCalendarBlock): CalendarBlock {
  return {
    id: block.id.toString(),
    taskId: block.taskId.toString(),
    taskStatus: block.taskStatus,
    taskDeadlineAt: block.taskDeadlineAt ?? null,
    taskPlannedDate: block.taskPlannedDate,
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

export async function moveTaskDay(taskId: string, sourceDate: string, targetDate: string): Promise<CalendarBlock[]> {
  const blocks = await apiRequest<ApiCalendarBlock[]>(`${BASE}/move-task-day`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ taskId: Number(taskId), sourceDate, targetDate }),
  });
  return blocks.map(normalizeCalendarBlock);
}

export interface RemovedTaskDay {
  plannedDates: Record<string, string | null>;
  removedBlockIds: string[];
  updatedBlocks: CalendarBlock[];
  scheduledTaskIds: string[];
}

export async function removeTaskDay(taskId: string, plannedDate: string): Promise<RemovedTaskDay> {
  const result = await apiRequest<{ plannedDates: Record<string, string | null>; removedBlockIds: number[]; updatedBlocks: ApiCalendarBlock[]; scheduledTaskIds: number[] }>(`${BASE}/remove-task-day`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ taskId: Number(taskId), plannedDate }),
  });
  return { ...result, removedBlockIds: result.removedBlockIds.map(String), updatedBlocks: result.updatedBlocks.map(normalizeCalendarBlock), scheduledTaskIds: result.scheduledTaskIds.map(String) };
}

export async function unscheduleCalendarBlock(blockId: string, plannedDate: string) {
  return apiRequest<{ taskId: number; plannedDate: string; scheduledTaskIds: number[] }>(`${BASE}/${blockId}/unschedule`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ plannedDate }),
  });
}
