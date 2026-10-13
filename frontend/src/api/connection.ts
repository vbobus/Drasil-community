const BACKEND_URL_KEY = 'drasil.backend-url.v1';
const SESSION_TOKEN_KEY = 'drasil.session-token.v1';
const ACCOUNT_KEY = 'drasil.account.v1';

export const DRASIL_UNAUTHORIZED_EVENT = 'drasil:unauthorized';

export interface DrasilAccount {
  id: string;
  username: string;
  displayName: string;
  isAdmin: boolean;
}

function configuredDefaultBackendUrl() {
  const meta = import.meta as ImportMeta & {
    readonly env?: { readonly VITE_DRASIL_BACKEND_URL?: string };
  };
  return meta.env?.VITE_DRASIL_BACKEND_URL?.trim() ?? '';
}

export function normalizeBackendUrl(value: string): string {
  const trimmed = value.trim();
  if (!trimmed) return '';

  const parsed = new URL(trimmed);
  if (!['http:', 'https:'].includes(parsed.protocol)) {
    throw new Error('The backend address must start with http:// or https://.');
  }
  if (parsed.username || parsed.password || parsed.search || parsed.hash) {
    throw new Error('The backend address cannot contain credentials, a query, or a fragment.');
  }
  if (window.location.protocol === 'https:' && parsed.protocol !== 'https:') {
    throw new Error('An HTTPS frontend requires an HTTPS backend.');
  }
  return parsed.toString().replace(/\/$/, '');
}

export function getBackendUrl(): string {
  const environment = (import.meta as ImportMeta & { env?: { VITE_DRASIL_HOSTED?: string } }).env;
  if (environment?.VITE_DRASIL_HOSTED === '1') return '';
  const stored = window.localStorage.getItem(BACKEND_URL_KEY);
  if (stored !== null) return stored;
  try {
    return normalizeBackendUrl(configuredDefaultBackendUrl());
  } catch {
    return '';
  }
}

export function setBackendUrl(value: string): string {
  const normalized = normalizeBackendUrl(value);
  window.localStorage.setItem(BACKEND_URL_KEY, normalized);
  return normalized;
}

export function backendDisplayName(): string {
  const backendUrl = getBackendUrl();
  return backendUrl || `${window.location.origin} (this server)`;
}

export function getSessionToken(): string | null {
  return window.sessionStorage.getItem(SESSION_TOKEN_KEY);
}

export function setSession(token: string, account: DrasilAccount): void {
  window.sessionStorage.setItem(SESSION_TOKEN_KEY, token);
  window.sessionStorage.setItem(ACCOUNT_KEY, JSON.stringify(account));
}

export function clearSession(): void {
  for (const key of Object.keys(window.sessionStorage)) {
    if (key.startsWith('drasil.workspace.v1:')) window.sessionStorage.removeItem(key);
  }
  window.dispatchEvent(new Event('drasil:clear-workspace'));
  window.sessionStorage.removeItem(SESSION_TOKEN_KEY);
  window.sessionStorage.removeItem(ACCOUNT_KEY);
}

export function getRememberedAccount(): DrasilAccount | null {
  const raw = window.sessionStorage.getItem(ACCOUNT_KEY);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as DrasilAccount;
  } catch {
    return null;
  }
}

export function apiUrl(input: RequestInfo | URL): RequestInfo | URL {
  if (typeof input !== 'string' || !input.startsWith('/api/')) return input;
  return `${getBackendUrl()}${input}`;
}

export async function apiFetch(
  input: RequestInfo | URL,
  init: RequestInit = {},
): Promise<Response> {
  const headers = new Headers(init.headers);
  const token = getSessionToken();
  const isDrasilApiPath = typeof input === 'string' && input.startsWith('/api/');
  if (token && isDrasilApiPath && !headers.has('Authorization')) {
    headers.set('Authorization', `Bearer ${token}`);
  }
  const response = await fetch(apiUrl(input), { ...init, headers });
  if (
    response.status === 401
    && typeof input === 'string'
    && !input.includes('/accounts/login')
  ) {
    window.dispatchEvent(new CustomEvent(DRASIL_UNAUTHORIZED_EVENT));
  }
  return response;
}
