import { apiRequest } from './http';
import type { ApiNode } from './nodes';
import { normalizeCalendarBlock } from './calendarBlocks';
import type { ApiCalendarBlock } from '../types/calendar';
import type { Category } from '../types/category';
import { localDayKey, mondayOf, shiftDay } from '../lib/weekPlanning';

export async function fetchWorkspace() {
  const monday = mondayOf(localDayKey(new Date()));
  const params = new URLSearchParams({ start: `${shiftDay(monday, -14)}T00:00:00`, end: `${shiftDay(monday, 35)}T00:00:00` });
  const response = await apiRequest<{ categories: Category[]; nodes: ApiNode[]; blocks: ApiCalendarBlock[];
    scheduledTaskIds: number[]; startAt: string; endAt: string }>(`/api/v1/workspace?${params}`);
  return { ...response, blocks: response.blocks.map(normalizeCalendarBlock), scheduledTaskIds: response.scheduledTaskIds.map(String) };
}
