import { create } from 'zustand';
import * as calendarBlocksApi from '../api/calendarBlocks';
import * as plannerApi from '../api/planner';
import * as schedulingApi from '../api/scheduling';
import type {
  CalendarBlock,
  CalendarBlockTimeUpdate,
  CalendarViewMode,
  ManualCalendarBlockCreate,
} from '../types/calendar';
import type {
  AutoScheduleDraftRequest,
  AutoScheduleDraftResult,
} from '../types/scheduling';
import type {
  PlannerProposeRequest,
  PlannerProposeResult,
  PlannerSchedulePatchProposal,
  SchedulePatchOperation,
} from '../types/planner';

interface CalendarState {
  blocks: CalendarBlock[];
  scheduledTaskIds: string[];
  draftBlocks: CalendarBlock[];
  viewMode: CalendarViewMode;
  detailLevel: number;
  isLoading: boolean;
  isScheduling: boolean;
  isPlannerProposing: boolean;
  error: string | null;
  scheduleError: string | null;
  plannerProposalError: string | null;
  scheduleDraftResult: AutoScheduleDraftResult | null;
  schedulePatchProposal: PlannerProposeResult | null;
  lastRange: { startAt: string; endAt: string } | null;
}

interface CalendarActions {
  fetchBlocks: (startAt: string, endAt: string) => Promise<void>;
  createManualBlock: (data: ManualCalendarBlockCreate) => Promise<CalendarBlock>;
  updateBlockTime: (blockId: string, data: CalendarBlockTimeUpdate) => Promise<CalendarBlock>;
  deleteBlock: (blockId: string) => Promise<void>;
  runAutoScheduleDraft: (data: AutoScheduleDraftRequest) => Promise<AutoScheduleDraftResult>;
  runPlannerScheduleProposal: (data: PlannerProposeRequest) => Promise<PlannerProposeResult>;
  ingestSharedScheduleProposal: (result: PlannerProposeResult) => void;
  markSchedulePatchProposalApplied: () => void;
  setDraftBlocks: (blocks: CalendarBlock[]) => void;
  clearDraftBlocks: () => void;
  updateDraftBlockTime: (blockId: string, data: CalendarBlockTimeUpdate) => void;
  removeDraftBlock: (blockId: string) => void;
  clearScheduleDraft: () => void;
  clearSchedulePatchProposal: () => void;
  rejectSchedulePatchProposal: () => void;
  setViewMode: (viewMode: CalendarViewMode) => void;
  setDetailLevel: (detailLevel: number) => void;
  clearError: () => void;
}

type CalendarStore = CalendarState & CalendarActions;

const pendingTimeUpdateVersions = new Map<string, number>();
const DEFAULT_SCHEDULE_SLOT_MINUTES = 15;
let calendarFetchRequestVersion = 0;
let scheduleDraftRequestVersion = 0;
let plannerProposalRequestVersion = 0;

function startOfLocalDay(date: Date) {
  const nextDate = new Date(date);
  nextDate.setHours(0, 0, 0, 0);
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

function defaultScheduleRange() {
  const start = roundUpToSlot(new Date(), DEFAULT_SCHEDULE_SLOT_MINUTES);
  const end = startOfLocalDay(start);
  end.setDate(end.getDate() + 1);
  return {
    startAt: toLocalIsoMinute(start),
    endAt: toLocalIsoMinute(end),
  };
}

function addDays(date: Date, days: number) {
  const nextDate = new Date(date);
  nextDate.setDate(nextDate.getDate() + days);
  return nextDate;
}

function roundUpToSlot(date: Date, slotMinutes: number) {
  const nextDate = new Date(date);
  const hadPartialMinute = nextDate.getSeconds() > 0 || nextDate.getMilliseconds() > 0;
  nextDate.setSeconds(0, 0);
  if (hadPartialMinute) {
    nextDate.setMinutes(nextDate.getMinutes() + 1);
  }
  const minutes = nextDate.getMinutes();
  const remainder = minutes % slotMinutes;
  if (remainder > 0) {
    nextDate.setMinutes(minutes + slotMinutes - remainder);
  }
  return nextDate;
}

function normalizeScheduleRange(
  range: { startAt: string; endAt: string },
  slotMinutes: number,
) {
  const requestedStart = new Date(range.startAt);
  const requestedEnd = new Date(range.endAt);
  if (Number.isNaN(requestedStart.getTime()) || Number.isNaN(requestedEnd.getTime())) {
    return range;
  }

  const nextAvailableStart = roundUpToSlot(new Date(), slotMinutes);
  if (requestedEnd <= nextAvailableStart) {
    const fallbackEnd = startOfLocalDay(addDays(nextAvailableStart, 1));
    return {
      startAt: toLocalIsoMinute(nextAvailableStart),
      endAt: toLocalIsoMinute(fallbackEnd),
    };
  }

  if (requestedStart < nextAvailableStart) {
    return {
      startAt: toLocalIsoMinute(nextAvailableStart),
      endAt: range.endAt,
    };
  }

  return range;
}

function sortBlocks(blocks: CalendarBlock[]): CalendarBlock[] {
  return [...blocks].sort((first, second) => {
    const firstStart = Date.parse(first.startAt);
    const secondStart = Date.parse(second.startAt);
    if (firstStart !== secondStart) {
      return firstStart - secondStart;
    }
    return first.id.localeCompare(second.id);
  });
}

function updatePlannerOperationTime(
  operation: SchedulePatchOperation,
  blockId: string,
  data: CalendarBlockTimeUpdate,
): SchedulePatchOperation {
  if (operation.proposalBlockId !== blockId) {
    return operation;
  }
  return {
    ...operation,
    startAt: data.startAt ?? operation.startAt,
    endAt: data.endAt ?? operation.endAt,
  };
}

function updatePlannerProposalTime(
  result: PlannerProposeResult | null,
  blockId: string,
  data: CalendarBlockTimeUpdate,
): PlannerProposeResult | null {
  if (!result?.proposal) {
    return result;
  }
  const proposal: PlannerSchedulePatchProposal = result.proposal;
  const dirtyWarning = 'This proposal changed locally and must be revalidated before approval.';
  return {
    ...result,
    proposal: {
      ...proposal,
      schedulePatch: {
        ...proposal.schedulePatch,
        operations: proposal.schedulePatch.operations.map((operation) =>
          updatePlannerOperationTime(operation, blockId, data),
        ),
      },
      schedulePatchSimulation: {
        ...proposal.schedulePatchSimulation,
        previewBlocks: sortBlocks(
          proposal.schedulePatchSimulation.previewBlocks.map((block) =>
            block.id === blockId
              ? {
                  ...block,
                  startAt: data.startAt ?? block.startAt,
                  endAt: data.endAt ?? block.endAt,
                }
              : block,
          ),
        ),
        warnings: Array.from(
          new Set([...(proposal.schedulePatchSimulation.warnings ?? []), dirtyWarning]),
        ),
      },
      schedulePatchValidation: {
        ...proposal.schedulePatchValidation,
        status: 'dirty',
        warnings: Array.from(
          new Set([...proposal.schedulePatchValidation.warnings, dirtyWarning]),
        ),
      },
      schedulePatchStatus: 'dirty',
    },
  };
}

function summarizeOperations(operations: SchedulePatchOperation[]) {
  const affectedTaskIds = Array.from(
    new Set(
      operations
        .map((operation) => operation.taskId)
        .filter((taskId): taskId is string => Boolean(taskId)),
    ),
  );
  const affectedCalendarBlockIds = Array.from(
    new Set(
      operations
        .map((operation) => operation.acceptedBlockId)
        .filter((blockId): blockId is number => typeof blockId === 'number'),
    ),
  );
  return {
    affectedTaskIds,
    affectedCalendarBlockIds,
    createBlocks: operations.filter((operation) => operation.op === 'create_block').length,
    updateBlockTimes: operations.filter((operation) => operation.op === 'update_block_time').length,
    removeBlocks: operations.filter((operation) => operation.op === 'remove_block').length,
    skipTasks: operations.filter((operation) => operation.op === 'skip_task').length,
  };
}

function removePlannerProposalBlock(
  result: PlannerProposeResult | null,
  blockId: string,
): PlannerProposeResult | null {
  if (!result?.proposal) {
    return result;
  }
  const proposal = result.proposal;
  const operations = proposal.schedulePatch.operations.filter(
    (operation) => operation.proposalBlockId !== blockId,
  );
  const previewBlocks = proposal.schedulePatchSimulation.previewBlocks.filter(
    (block) => block.id !== blockId,
  );
  const operationSummary = summarizeOperations(operations);
  const isEmpty = operations.length === 0;
  const dirtyWarning = 'This proposal changed locally and must be revalidated before approval.';
  return {
    ...result,
    proposal: {
      ...proposal,
      preview: previewBlocks.map(
        (block) => `${block.title}, ${block.startAt} - ${block.endAt}`,
      ),
      schedulePatch: {
        ...proposal.schedulePatch,
        operations,
      },
      schedulePatchValidation: {
        ...proposal.schedulePatchValidation,
        status: isEmpty ? 'invalid' : 'dirty',
        errors: isEmpty ? ['SchedulePatch has no operations.'] : [],
        warnings: isEmpty ? [] : [dirtyWarning],
        operationCount: operations.length,
      },
      schedulePatchSimulation: {
        ...proposal.schedulePatchSimulation,
        operationCount: operations.length,
        previewBlocks,
        warnings: isEmpty ? [] : [dirtyWarning],
        ...operationSummary,
      },
      schedulePatchStatus: isEmpty ? 'invalid' : 'dirty',
    },
  };
}

export const useCalendarStore = create<CalendarStore>((set) => ({
  blocks: [],
  scheduledTaskIds: [],
  draftBlocks: [],
  viewMode: 'day',
  detailLevel: 0.65,
  isLoading: false,
  isScheduling: false,
  isPlannerProposing: false,
  error: null,
  scheduleError: null,
  plannerProposalError: null,
  scheduleDraftResult: null,
  schedulePatchProposal: null,
  lastRange: null,

  fetchBlocks: async (startAt, endAt) => {
    const requestVersion = ++calendarFetchRequestVersion;
    set({
      isLoading: true,
      error: null,
      lastRange: { startAt, endAt },
    });
    try {
      const [blocks, scheduledTaskIds] = await Promise.all([
        calendarBlocksApi.fetchCalendarBlocks(startAt, endAt),
        calendarBlocksApi.fetchScheduledTaskIds(),
      ]);
      if (requestVersion !== calendarFetchRequestVersion) {
        return;
      }
      set({
        blocks: sortBlocks(blocks),
        scheduledTaskIds,
        isLoading: false,
      });
    } catch (error) {
      if (requestVersion !== calendarFetchRequestVersion) {
        return;
      }
      const message = error instanceof Error ? error.message : 'Failed to load calendar blocks.';
      set({ error: message, isLoading: false });
    }
  },

  createManualBlock: async (data) => {
    set({ error: null });
    try {
      const createdBlock = await calendarBlocksApi.createCalendarBlock(data);
      set((state) => ({
        blocks: sortBlocks([...state.blocks, createdBlock]),
        scheduledTaskIds: Array.from(
          new Set([...state.scheduledTaskIds, createdBlock.taskId]),
        ),
      }));
      return createdBlock;
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Failed to create calendar block.';
      set({ error: message });
      throw error instanceof Error ? error : new Error(message);
    }
  },

  updateBlockTime: async (blockId, data) => {
    const requestVersion = (pendingTimeUpdateVersions.get(blockId) ?? 0) + 1;
    pendingTimeUpdateVersions.set(blockId, requestVersion);
    let previousBlock: CalendarBlock | null = null;

    set((state) => ({
      error: null,
      blocks: sortBlocks(
        state.blocks.map((block) => {
          if (block.id !== blockId) {
            return block;
          }
          previousBlock = block;
          return {
            ...block,
            startAt: data.startAt ?? block.startAt,
            endAt: data.endAt ?? block.endAt,
          };
        }),
      ),
    }));

    try {
      const updatedBlock = await calendarBlocksApi.updateCalendarBlock(blockId, data);
      if (pendingTimeUpdateVersions.get(blockId) !== requestVersion) {
        return updatedBlock;
      }
      pendingTimeUpdateVersions.delete(blockId);
      set((state) => ({
        blocks: sortBlocks(
          state.blocks.map((block) => (block.id === blockId ? updatedBlock : block)),
        ),
      }));
      return updatedBlock;
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Failed to update calendar block.';
      if (pendingTimeUpdateVersions.get(blockId) === requestVersion) {
        pendingTimeUpdateVersions.delete(blockId);
        const blockToRestore = previousBlock;
        set((state) => ({
          error: message,
          blocks: blockToRestore
            ? sortBlocks(
                state.blocks.map((block) => (block.id === blockId ? blockToRestore : block)),
              )
            : state.blocks,
        }));
      } else {
        set({ error: message });
      }
      throw error instanceof Error ? error : new Error(message);
    }
  },

  deleteBlock: async (blockId) => {
    set({ error: null });
    try {
      await calendarBlocksApi.deleteCalendarBlock(blockId);
      let scheduledTaskIds: string[] | null = null;
      try {
        scheduledTaskIds = await calendarBlocksApi.fetchScheduledTaskIds();
      } catch {
        // The block deletion succeeded. Keep the last membership snapshot until
        // the next calendar refresh instead of surfacing a false delete failure.
      }
      set((state) => ({
        blocks: state.blocks.filter((block) => block.id !== blockId),
        scheduledTaskIds: scheduledTaskIds ?? state.scheduledTaskIds,
        draftBlocks: state.draftBlocks.filter((block) => block.id !== blockId),
        schedulePatchProposal: removePlannerProposalBlock(
          state.schedulePatchProposal,
          blockId,
        ),
      }));
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Failed to delete calendar block.';
      set({ error: message });
      throw error instanceof Error ? error : new Error(message);
    }
  },

  runAutoScheduleDraft: async (data) => {
    const requestVersion = ++scheduleDraftRequestVersion;
    const slotMinutes = data.slotMinutes ?? DEFAULT_SCHEDULE_SLOT_MINUTES;
    const range = getRangeForSchedule(slotMinutes);
    set({
      isScheduling: true,
      scheduleError: null,
      plannerProposalError: null,
      scheduleDraftResult: null,
      schedulePatchProposal: null,
      draftBlocks: [],
    });
    try {
      const result = await schedulingApi.autoScheduleDraft({
        slotMinutes,
        fallbackDurationMinutes: 30,
        ...data,
        windowStart: data.windowStart ?? range.startAt,
        windowEnd: data.windowEnd ?? range.endAt,
      });
      if (requestVersion !== scheduleDraftRequestVersion) {
        return result;
      }
      set({
        draftBlocks: sortBlocks(result.draftBlocks),
        scheduleDraftResult: result,
        schedulePatchProposal: null,
        isScheduling: false,
      });
      return result;
    } catch (error) {
      const message =
        error instanceof Error ? error.message : 'Failed to draft a schedule preview.';
      if (requestVersion === scheduleDraftRequestVersion) {
        set({
          scheduleError: message,
          isScheduling: false,
          draftBlocks: [],
        });
      }
      throw error instanceof Error ? error : new Error(message);
    }
  },

  runPlannerScheduleProposal: async (data) => {
    const requestVersion = ++plannerProposalRequestVersion;
    const slotMinutes = data.slotMinutes ?? DEFAULT_SCHEDULE_SLOT_MINUTES;
    const range = getRangeForSchedule(slotMinutes);
    set({
      isPlannerProposing: true,
      plannerProposalError: null,
      schedulePatchProposal: null,
      scheduleDraftResult: null,
      scheduleError: null,
      draftBlocks: [],
    });
    try {
      const result = await plannerApi.proposeSchedulePatch({
        slotMinutes,
        fallbackDurationMinutes: 30,
        ...data,
        windowStart: data.windowStart ?? range.startAt,
        windowEnd: data.windowEnd ?? range.endAt,
      });
      if (requestVersion !== plannerProposalRequestVersion) {
        return result;
      }
      const previewBlocks = result.proposal?.schedulePatchSimulation.previewBlocks ?? [];
      set({
        draftBlocks: sortBlocks(previewBlocks),
        schedulePatchProposal: result,
        isPlannerProposing: false,
      });
      return result;
    } catch (error) {
      const message =
        error instanceof Error ? error.message : 'Failed to prepare a schedule proposal.';
      if (requestVersion === plannerProposalRequestVersion) {
        set({
          plannerProposalError: message,
          isPlannerProposing: false,
          draftBlocks: [],
        });
      }
      throw error instanceof Error ? error : new Error(message);
    }
  },

  ingestSharedScheduleProposal: (result) => {
    const previewBlocks = result.proposal?.schedulePatchSimulation.previewBlocks ?? [];
    set({
      draftBlocks: sortBlocks(previewBlocks),
      schedulePatchProposal: result,
      plannerProposalError: null,
      scheduleDraftResult: null,
      isPlannerProposing: false,
    });
  },

  markSchedulePatchProposalApplied: () => {
    set((state) => {
      const currentResult = state.schedulePatchProposal;
      const proposal = currentResult?.proposal;
      if (!currentResult || !proposal) {
        return { draftBlocks: [] };
      }
      const payload = proposal.payload ?? {};
      const currentLifecycle = (
        payload.schedulePatchLifecycle
        && typeof payload.schedulePatchLifecycle === 'object'
        && !Array.isArray(payload.schedulePatchLifecycle)
      )
        ? payload.schedulePatchLifecycle as Record<string, unknown>
        : {};
      return {
        draftBlocks: [],
        schedulePatchProposal: {
          ...currentResult,
          proposal: {
            ...proposal,
            payload: {
              ...payload,
              schedulePatchLifecycle: {
                ...currentLifecycle,
                status: 'applied',
              },
            },
          },
        },
      };
    });
  },

  setDraftBlocks: (blocks) => {
    set({ draftBlocks: sortBlocks(blocks) });
  },

  clearDraftBlocks: () => {
    set({ draftBlocks: [] });
  },

  updateDraftBlockTime: (blockId, data) => {
    set((state) => {
      const updateBlock = (block: CalendarBlock) =>
        block.id === blockId
          ? {
              ...block,
              startAt: data.startAt ?? block.startAt,
              endAt: data.endAt ?? block.endAt,
            }
          : block;
      const draftBlocks = sortBlocks(state.draftBlocks.map(updateBlock));
      return {
        draftBlocks,
        scheduleDraftResult: state.scheduleDraftResult
          ? {
              ...state.scheduleDraftResult,
              draftBlocks: sortBlocks(state.scheduleDraftResult.draftBlocks.map(updateBlock)),
            }
          : state.scheduleDraftResult,
        schedulePatchProposal: updatePlannerProposalTime(
          state.schedulePatchProposal,
          blockId,
          data,
        ),
      };
    });
  },

  removeDraftBlock: (blockId) => {
    set((state) => ({
      draftBlocks: state.draftBlocks.filter((block) => block.id !== blockId),
      scheduleDraftResult: state.scheduleDraftResult
        ? {
            ...state.scheduleDraftResult,
            draftBlocks: state.scheduleDraftResult.draftBlocks.filter(
              (block) => block.id !== blockId,
            ),
            diagnostics: {
              ...state.scheduleDraftResult.diagnostics,
              scheduledTaskCount: Math.max(
                0,
                state.scheduleDraftResult.diagnostics.scheduledTaskCount - 1,
              ),
            },
          }
        : state.scheduleDraftResult,
      schedulePatchProposal: removePlannerProposalBlock(state.schedulePatchProposal, blockId),
    }));
  },

  clearScheduleDraft: () => {
    scheduleDraftRequestVersion += 1;
    plannerProposalRequestVersion += 1;
    set({
      draftBlocks: [],
      scheduleDraftResult: null,
      schedulePatchProposal: null,
      scheduleError: null,
      plannerProposalError: null,
      isScheduling: false,
      isPlannerProposing: false,
    });
  },

  clearSchedulePatchProposal: () => {
    plannerProposalRequestVersion += 1;
    set({
      draftBlocks: [],
      schedulePatchProposal: null,
      plannerProposalError: null,
      isPlannerProposing: false,
    });
  },

  rejectSchedulePatchProposal: () => {
    plannerProposalRequestVersion += 1;
    set({
      draftBlocks: [],
      schedulePatchProposal: null,
      plannerProposalError: null,
      isPlannerProposing: false,
    });
  },

  setViewMode: (viewMode) => {
    set({ viewMode });
  },

  setDetailLevel: (detailLevel) => {
    set({ detailLevel: Math.max(0, Math.min(1, detailLevel)) });
  },

  clearError: () => {
    set({ error: null });
  },
}));

function getRangeForSchedule(slotMinutes: number = DEFAULT_SCHEDULE_SLOT_MINUTES) {
  return normalizeScheduleRange(
    useCalendarStore.getState().lastRange ?? defaultScheduleRange(),
    slotMinutes,
  );
}
