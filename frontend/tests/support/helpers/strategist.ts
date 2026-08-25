import type { Page } from '@playwright/test';
import type {
  StrategistResponse,
  StrategistSessionDetail,
  StrategistSessionSummary,
} from '../../../src/api/strategist';

export interface CapturedStrategistRequest {
  prompt?: string | null;
  sessionId?: string | null;
  activeCategoryId?: number | null;
  focusNodeId?: number | null;
  stagedNodeIds?: number[];
  calendarViewStart?: string | null;
  calendarViewEnd?: string | null;
  localDateTime?: string | null;
  timeZone?: string | null;
  uiEvent?: {
    kind: 'clarification_choice' | 'proposal_action' | 'focus_change';
    sourceTurnId?: string;
    targetNodeId?: number;
    targetNodeIds?: number[];
    proposalId?: string;
    proposalKind?: string;
    goalId?: string;
    goalRevision?: number;
    decisionId?: string;
    optionId?: string;
    eventId?: string;
    idempotencyKey?: string;
    action?: 'approve' | 'edit' | 'reject';
    payload?: Record<string, unknown>;
  };
}

export interface CapturedStrategistContextRequest {
  nodeIds: number[];
  source?: string;
  mentionText?: string | null;
}

export interface MockStrategistTurnRecord {
  id: string;
  prompt: string;
  response?: StrategistResponse | null;
  error?: string | null;
  createdAt: string;
}

export interface MockStrategistRunResponse {
  session: StrategistSessionSummary;
  turn: MockStrategistTurnRecord;
}

export interface MockStrategistApiOptions {
  sessions?: StrategistSessionSummary[];
  sessionDetails?: Record<string, StrategistSessionDetail>;
  runResponses?: MockStrategistRunResponse[];
  runResponseDelayMs?: number;
  sessionDetailDelayMs?: Record<string, number>;
}

export interface MockStrategistApiResult {
  capturedRunRequests: CapturedStrategistRequest[];
  capturedContextRequests: CapturedStrategistContextRequest[];
}

async function fulfillJson(page: Page, url: string | RegExp, handler: Parameters<Page['route']>[1]) {
  await page.route(url, handler);
}

function delay(ms: number) {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

export async function mockStrategistApi(
  page: Page,
  options: MockStrategistApiOptions = {},
): Promise<MockStrategistApiResult> {
  const capturedRunRequests: CapturedStrategistRequest[] = [];
  const capturedContextRequests: CapturedStrategistContextRequest[] = [];
  const runResponses = [...(options.runResponses ?? [])];
  const sessions = options.sessions ?? [];
  const sessionDetails = options.sessionDetails ?? {};

  await fulfillJson(page, '**/api/v1/ai/strategist/sessions', async (route) => {
    if (route.request().method() !== 'GET') {
      await route.fulfill({ status: 405 });
      return;
    }

    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(sessions),
    });
  });

  await fulfillJson(page, /\/api\/v1\/ai\/strategist\/sessions\/[^/]+$/, async (route) => {
    if (route.request().method() !== 'GET') {
      await route.fulfill({ status: 405 });
      return;
    }

    const sessionId = route.request().url().split('/').at(-1) ?? '';
    const sessionDetail = sessionDetails[sessionId];

    const sessionDelayMs = options.sessionDetailDelayMs?.[sessionId] ?? 0;
    if (sessionDelayMs > 0) {
      await delay(sessionDelayMs);
    }

    if (!sessionDetail) {
      await route.fulfill({
        status: 404,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'Strategist session not found.' }),
      });
      return;
    }

    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(sessionDetail),
    });
  });

  await fulfillJson(page, '**/api/v1/ai/strategist', async (route) => {
    if (route.request().method() !== 'POST') {
      await route.fulfill({ status: 405 });
      return;
    }

    capturedRunRequests.push((await route.request().postDataJSON()) as CapturedStrategistRequest);
    const nextResponse = runResponses.shift();

    if (options.runResponseDelayMs && options.runResponseDelayMs > 0) {
      await delay(options.runResponseDelayMs);
    }

    if (!nextResponse) {
      await route.fulfill({
        status: 500,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'No mocked strategist response queued.' }),
      });
      return;
    }

    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(nextResponse),
    });
  });

  await fulfillJson(page, /\/api\/v1\/ai\/strategist\/sessions\/[^/]+\/context$/, async (route) => {
    if (route.request().method() !== 'POST') {
      await route.fulfill({ status: 405 });
      return;
    }

    capturedContextRequests.push(
      (await route.request().postDataJSON()) as CapturedStrategistContextRequest,
    );
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ ok: true }),
    });
  });

  return {
    capturedRunRequests,
    capturedContextRequests,
  };
}
