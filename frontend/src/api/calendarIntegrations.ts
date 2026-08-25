import { apiRequest } from './http';

const BASE = '/api/v1/calendar-integrations/google';

export interface GoogleCalendarStatus {
  provider: 'google';
  isConfigured: boolean;
  isConnected: boolean;
  requiresReconnect?: boolean;
  calendarId?: string | null;
  calendarSummary?: string | null;
  accountEmail?: string | null;
  timeZone?: string | null;
  lastSyncedAt?: string | null;
  lastSyncError?: string | null;
  scopes: string[];
  syncPolicy: {
    sourceOfTruth: string;
    googleTimeEdits: string;
    googleTitleAndDescriptionEdits: string;
    completedTitlePrefix: string;
    calendarMode: string;
  };
}

export interface GoogleCalendarConnectResponse {
  authorizationUrl: string;
  state: string;
  scopes: string[];
  redirectUri: string;
}

export interface GoogleCalendarSyncResponse {
  direction: 'push' | 'pull' | 'both';
  pull?: { updated: number; scanned: number };
  push?: { pushed: number; failed: number };
  status: GoogleCalendarStatus;
}

export async function fetchGoogleCalendarStatus(): Promise<GoogleCalendarStatus> {
  return apiRequest<GoogleCalendarStatus>(`${BASE}/status`);
}

export async function startGoogleCalendarConnect(): Promise<GoogleCalendarConnectResponse> {
  return apiRequest<GoogleCalendarConnectResponse>(`${BASE}/connect`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({}),
  });
}

export async function disconnectGoogleCalendar(): Promise<GoogleCalendarStatus> {
  return apiRequest<GoogleCalendarStatus>(BASE, { method: 'DELETE' });
}

export async function syncGoogleCalendar(
  direction: 'push' | 'pull' | 'both' = 'both',
): Promise<GoogleCalendarSyncResponse> {
  return apiRequest<GoogleCalendarSyncResponse>(`${BASE}/sync`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ direction }),
  });
}
