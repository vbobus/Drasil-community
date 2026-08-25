import type { Page, Route } from '@playwright/test';
import type { ApiNode } from '../../../src/api/nodes';
import type { Category } from '../../../src/types/category';

export interface MockWorkspaceRoutes {
  categories: Category[];
  nodesByCategoryId: Record<number, ApiNode[]>;
}

export interface MockCalendarBlock {
  id: number;
  taskId: number;
  taskStatus?: string;
  taskDeadlineAt?: string | null;
  taskPriority?: 'low' | 'medium' | 'high';
  timeEstimateMinutes?: number | null;
  categoryId: number;
  title: string;
  emoji?: string | null;
  categoryName: string;
  categoryColor: string;
  startAt: string;
  endAt: string;
  variant: 'scheduled' | 'protected' | 'draft' | 'proposed';
  completionStatus: string;
  isPinned: boolean;
  isProtected: boolean;
  isRecurring: boolean;
  hierarchyDepth?: number;
  renderedChildTaskIds?: number[];
}

export interface MockCalendarRoutes {
  blocks?: MockCalendarBlock[];
  createBlock?: (payload: Record<string, unknown>) => MockCalendarBlock;
  updateBlock?: (
    block: MockCalendarBlock,
    payload: Record<string, unknown>,
  ) => MockCalendarBlock | Promise<MockCalendarBlock>;
  deleteBlock?: (block: MockCalendarBlock) => void;
}

export interface MockDraftCalendarBlock extends Omit<MockCalendarBlock, 'id' | 'variant'> {
  id: string;
  variant: 'draft';
}

export interface MockScheduleConflict {
  taskId: number;
  type: string;
  detail: string;
  conflictingBlockIds: number[];
  alternatives: Array<{
    startAt: string;
    endAt: string;
    reason: string;
  }>;
}

export interface MockScheduleDryRunResponse {
  draftBlocks: MockDraftCalendarBlock[];
  conflicts: MockScheduleConflict[];
  diagnostics: {
    solverStatus: string;
    runtimeMs: number;
    scheduledTaskCount: number;
    conflictCount: number;
    occupiedBlockCount: number;
    fallbackDurationTaskIds: number[];
    alreadyScheduledTaskIds: number[];
  };
}

export interface MockScheduleRoutes {
  response: MockScheduleDryRunResponse;
}

export interface MockPlannerProposalResponse {
  providerLabel: string;
  mode: string;
  summary: string;
  contextCards: Array<{
    id: string;
    label: string;
    value: string;
    detail?: string | null;
  }>;
  suggestedPrompts: string[];
  proposal: Record<string, unknown> | null;
  diagnostics: Record<string, unknown>;
}

export interface MockPlannerRoutes {
  response: MockPlannerProposalResponse;
  delayMs?: number;
}

async function fulfillJson(route: Route, body: unknown, status = 200) {
  await route.fulfill({
    status,
    contentType: 'application/json',
    body: JSON.stringify(body),
  });
}

export async function mockWorkspace(page: Page, routes: MockWorkspaceRoutes) {
  await page.route('**/api/v1/categories', async (route) => {
    if (route.request().method() !== 'GET') {
      await fulfillJson(route, { error: { detail: 'Unsupported categories method in test.' } }, 405);
      return;
    }

    await fulfillJson(route, routes.categories);
  });

  await page.route(/\/api\/v1\/nodes\?.*categoryId=\d+/, async (route) => {
    if (route.request().method() !== 'GET') {
      await fulfillJson(route, { error: { detail: 'Unsupported nodes method in test.' } }, 405);
      return;
    }

    const requestUrl = new URL(route.request().url());
    const categoryId = Number(requestUrl.searchParams.get('categoryId'));

    if (!Number.isFinite(categoryId)) {
      await fulfillJson(route, { error: { detail: 'Missing categoryId test route.' } }, 400);
      return;
    }

    await fulfillJson(route, routes.nodesByCategoryId[categoryId] ?? []);
  });
}

export async function mockCalendarBlocks(page: Page, routes: MockCalendarRoutes = {}) {
  const blocks = [...(routes.blocks ?? [])];
  const createdPayloads: Record<string, unknown>[] = [];
  const updatedPayloads: { id: number; payload: Record<string, unknown> }[] = [];
  const deletedIds: number[] = [];

  await page.route('**/api/v1/calendar-blocks**', async (route) => {
    const method = route.request().method();
    const requestUrl = new URL(route.request().url());
    const maybeBlockId = Number(requestUrl.pathname.split('/').at(-1));

    if (method === 'GET') {
      if (requestUrl.pathname.endsWith('/scheduled-task-ids')) {
        await fulfillJson(
          route,
          Array.from(
            new Set(
              blocks
                .filter((block) => block.variant === 'scheduled' || block.variant === 'protected')
                .map((block) => block.taskId),
            ),
          ),
        );
        return;
      }
      const rangeStart = requestUrl.searchParams.get('start');
      const rangeEnd = requestUrl.searchParams.get('end');
      const startMs = rangeStart ? Date.parse(rangeStart) : Number.NEGATIVE_INFINITY;
      const endMs = rangeEnd ? Date.parse(rangeEnd) : Number.POSITIVE_INFINITY;
      await fulfillJson(
        route,
        blocks.filter((block) =>
          Date.parse(block.startAt) < endMs && Date.parse(block.endAt) > startMs,
        ),
      );
      return;
    }

    if (method === 'POST') {
      const payload = route.request().postDataJSON() as Record<string, unknown>;
      createdPayloads.push(payload);
      const created =
        routes.createBlock?.(payload) ??
        ({
          id: 9000 + createdPayloads.length,
          taskId: Number(payload.taskId),
          categoryId: Number(payload.categoryId),
          title: 'Manually scheduled task',
          emoji: 'M',
          categoryName: 'Work',
          categoryColor: '#1f6feb',
          startAt: String(payload.startAt),
          endAt: String(payload.endAt),
          variant: 'scheduled',
          completionStatus: 'not_started',
          isPinned: false,
          isProtected: false,
          isRecurring: false,
          hierarchyDepth: 0,
          renderedChildTaskIds: [],
        } satisfies MockCalendarBlock);
      blocks.push(created);
      await fulfillJson(route, created, 201);
      return;
    }

    if (method === 'PATCH') {
      if (!Number.isFinite(maybeBlockId)) {
        await fulfillJson(route, { error: { detail: 'Missing calendar block id.' } }, 400);
        return;
      }

      const blockIndex = blocks.findIndex((block) => block.id === maybeBlockId);
      if (blockIndex < 0) {
        await fulfillJson(route, { error: { detail: 'Calendar block not found.' } }, 404);
        return;
      }

      const payload = route.request().postDataJSON() as Record<string, unknown>;
      updatedPayloads.push({ id: maybeBlockId, payload });
      const updated =
        routes.updateBlock?.(blocks[blockIndex], payload) ??
        ({
          ...blocks[blockIndex],
          startAt: typeof payload.startAt === 'string' ? payload.startAt : blocks[blockIndex].startAt,
          endAt: typeof payload.endAt === 'string' ? payload.endAt : blocks[blockIndex].endAt,
        } satisfies MockCalendarBlock);
      const resolvedUpdated = await updated;
      blocks[blockIndex] = resolvedUpdated;
      await fulfillJson(route, resolvedUpdated);
      return;
    }

    if (method === 'DELETE') {
      if (!Number.isFinite(maybeBlockId)) {
        await fulfillJson(route, { error: { detail: 'Missing calendar block id.' } }, 400);
        return;
      }

      const blockIndex = blocks.findIndex((block) => block.id === maybeBlockId);
      if (blockIndex < 0) {
        await fulfillJson(route, { error: { detail: 'Calendar block not found.' } }, 404);
        return;
      }

      const [deletedBlock] = blocks.splice(blockIndex, 1);
      routes.deleteBlock?.(deletedBlock);
      deletedIds.push(maybeBlockId);
      await route.fulfill({ status: 204 });
      return;
    }

    await fulfillJson(route, { error: { detail: 'Unsupported calendar method in test.' } }, 405);
  });

  return {
    blocks,
    createdPayloads,
    updatedPayloads,
    deletedIds,
  };
}

export async function mockScheduleDryRun(page: Page, routes: MockScheduleRoutes) {
  const requests: Record<string, unknown>[] = [];

  await page.route('**/api/v1/scheduling/auto-draft', async (route) => {
    if (route.request().method() !== 'POST') {
      await fulfillJson(route, { error: { detail: 'Unsupported scheduling method in test.' } }, 405);
      return;
    }

    const payload = route.request().postDataJSON() as Record<string, unknown>;
    requests.push(payload);
    await fulfillJson(route, routes.response);
  });

  return {
    requests,
  };
}

export async function mockPlannerProposal(page: Page, routes: MockPlannerRoutes) {
  const requests: Record<string, unknown>[] = [];

  await page.route('**/api/v1/ai/planner/propose', async (route) => {
    if (route.request().method() !== 'POST') {
      await fulfillJson(route, { error: { detail: 'Unsupported planner method in test.' } }, 405);
      return;
    }

    const payload = route.request().postDataJSON() as Record<string, unknown>;
    requests.push(payload);
    if (routes.delayMs) {
      await new Promise((resolve) => setTimeout(resolve, routes.delayMs));
    }
    await fulfillJson(route, routes.response);
  });

  return {
    requests,
  };
}

export function waitForWorkspaceBoot(page: Page, categoryId: number) {
  return Promise.all([
    page.waitForResponse(
      (response) =>
        response.request().method() === 'GET' &&
        response.url().endsWith('/api/v1/categories'),
    ),
    page.waitForResponse(
      (response) =>
        response.request().method() === 'GET' &&
        response.url().includes(`/api/v1/nodes?categoryId=${categoryId}`),
    ),
  ]);
}
