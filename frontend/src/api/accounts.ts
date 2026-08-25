import type { DrasilAccount } from './connection';
import { apiRequest } from './http';

const BASE = '/api/v1/accounts';

export interface AccountStatus {
  needsBootstrap: boolean;
  authenticationRequired: boolean;
  supportsRemoteBackend: boolean;
}

export interface AccountSession {
  token: string;
  account: DrasilAccount;
}

export interface AccountCredentials {
  username: string;
  password: string;
}

export function fetchAccountStatus(): Promise<AccountStatus> {
  return apiRequest<AccountStatus>(`${BASE}/status`);
}

export function bootstrapAccount(
  credentials: AccountCredentials & {
    displayName: string;
    bootstrapToken?: string;
  },
): Promise<AccountSession> {
  return apiRequest<AccountSession>(`${BASE}/bootstrap`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(credentials),
  });
}

export function loginAccount(credentials: AccountCredentials): Promise<AccountSession> {
  return apiRequest<AccountSession>(`${BASE}/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(credentials),
  });
}

export function logoutAccount(): Promise<void> {
  return apiRequest<void>(`${BASE}/logout`, { method: 'POST' });
}

export function fetchCurrentAccount(): Promise<DrasilAccount> {
  return apiRequest<DrasilAccount>(`${BASE}/me`);
}

export function fetchAccounts(): Promise<DrasilAccount[]> {
  return apiRequest<DrasilAccount[]>(BASE);
}

export function createAccount(
  credentials: AccountCredentials & {
    displayName: string;
    isAdmin?: boolean;
  },
): Promise<DrasilAccount> {
  return apiRequest<DrasilAccount>(BASE, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(credentials),
  });
}

export function changePassword(
  currentPassword: string,
  newPassword: string,
): Promise<AccountSession> {
  return apiRequest<AccountSession>(`${BASE}/password`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ currentPassword, newPassword }),
  });
}
