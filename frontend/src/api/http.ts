type JsonObject = Record<string, unknown>;

export class ApiError extends Error {
  readonly status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

function asObject(value: unknown): JsonObject | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as JsonObject)
    : null;
}

function errorDetail(value: unknown): string | null {
  const object = asObject(value);
  return typeof object?.detail === 'string' ? object.detail : null;
}

function responseErrorMessage(payload: unknown, status: number): string {
  const body = asObject(payload);
  if (!body) return `HTTP ${status}`;

  const directError = errorDetail(body.error);
  if (directError) return directError;

  if (typeof body.detail === 'string') return body.detail;

  const nestedError = asObject(asObject(body.detail)?.error);
  const nestedDetail = errorDetail(nestedError);
  if (!nestedDetail) return `HTTP ${status}`;

  const existingDescription = nestedError?.existingDescription;
  return typeof existingDescription === 'string' && existingDescription.trim()
    ? `${nestedDetail} Existing memory: ${existingDescription}`
    : nestedDetail;
}

/**
 * Decode a JSON API response using Drasil's shared backend error envelope.
 * Successful 204 responses intentionally resolve to `undefined`.
 */
export async function readJson<T>(response: Response): Promise<T> {
  if (response.ok) {
    if (response.status === 204) return undefined as T;
    return response.json() as Promise<T>;
  }

  let message = `HTTP ${response.status}`;
  try {
    message = responseErrorMessage(await response.json(), response.status);
  } catch {
    // Keep the status-based fallback when the response has no JSON body.
  }
  throw new ApiError(message, response.status);
}

/** Execute one API request and decode its JSON response consistently. */
export async function apiRequest<T>(
  input: RequestInfo | URL,
  init?: RequestInit,
): Promise<T> {
  return readJson<T>(await apiFetch(input, init));
}
import { apiFetch } from './connection';
