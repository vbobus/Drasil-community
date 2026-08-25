import type {
  ApiAutoScheduleDraftResponse,
  AutoScheduleDraftRequest,
  AutoScheduleDraftResult,
} from '../types/scheduling';
import type { CalendarBlock } from '../types/calendar';
import { apiRequest } from './http';

const BASE = '/api/v1/scheduling';

function normalizeDraftBlock(block: ApiAutoScheduleDraftResponse['draftBlocks'][number]): CalendarBlock {
  return {
    id: block.id,
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
    hierarchyDepth: block.hierarchyDepth ?? 0,
    renderedChildTaskIds: (block.renderedChildTaskIds ?? []).map((id) => id.toString()),
  };
}

function normalizeResponse(response: ApiAutoScheduleDraftResponse): AutoScheduleDraftResult {
  return {
    draftBlocks: response.draftBlocks.map(normalizeDraftBlock),
    conflicts: response.conflicts.map((conflict) => ({
      ...conflict,
      taskId: conflict.taskId.toString(),
    })),
    diagnostics: {
      ...response.diagnostics,
      fallbackDurationTaskIds: response.diagnostics.fallbackDurationTaskIds.map((id) =>
        id.toString(),
      ),
      alreadyScheduledTaskIds: (response.diagnostics.alreadyScheduledTaskIds ?? []).map((id) =>
        id.toString(),
      ),
    },
  };
}

export async function autoScheduleDraft(
  data: AutoScheduleDraftRequest & { windowStart: string; windowEnd: string },
): Promise<AutoScheduleDraftResult> {
  const response = await apiRequest<ApiAutoScheduleDraftResponse>(`${BASE}/auto-draft`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      taskIds: data.taskIds.map((id) => parseInt(id, 10)),
      windowStart: data.windowStart,
      windowEnd: data.windowEnd,
      slotMinutes: data.slotMinutes,
      fallbackDurationMinutes: data.fallbackDurationMinutes,
    }),
  });
  return normalizeResponse(response);
}
