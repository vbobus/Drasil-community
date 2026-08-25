import { apiRequest } from './http';

const BASE = '/api/v1/preferences';

export interface RememberedConstraint {
  id: string;
  constraintType: string;
  targetType: string;
  targetId?: string | null;
  hard: boolean;
  active: boolean;
  description?: string | null;
  value: Record<string, unknown>;
  createdAt: string;
  updatedAt: string;
}

export interface RememberedPreference {
  id: string;
  revision: number;
  domain: string;
  preferenceType: string;
  scopeType: string;
  scopeId?: string | null;
  strength: string;
  source: string;
  confidence: number;
  active: boolean;
  description?: string | null;
  value: Record<string, unknown>;
  effectiveFrom?: string | null;
  effectiveUntil?: string | null;
  issue?: {
    reasonCode: string;
    reason: string;
    relatedPreferenceId?: string | null;
    relatedDescription?: string | null;
  } | null;
  constraints: RememberedConstraint[];
  createdAt: string;
  updatedAt: string;
}

export interface RememberedPreferencesSummary {
  activeCount: number;
  inactiveCount: number;
  hardConstraintCount: number;
  issueCount?: number;
}

export interface RememberedPreferencesResponse {
  preferences: RememberedPreference[];
  summary: RememberedPreferencesSummary;
}

export async function fetchRememberedPreferences(options?: {
  includeInactive?: boolean;
}): Promise<RememberedPreferencesResponse> {
  const query = options?.includeInactive ? '?includeInactive=true' : '';
  return apiRequest<RememberedPreferencesResponse>(`${BASE}/remembered${query}`);
}

export async function updateRememberedPreference(
  preferenceId: string,
  data: RememberedPreferenceUpdate,
): Promise<RememberedPreference> {
  return apiRequest<RememberedPreference>(`${BASE}/remembered/${preferenceId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
}

export interface RememberedPreferenceWrite {
  domain: string;
  preferenceType: string;
  scopeType: 'global' | 'task' | 'category';
  scopeId?: string | null;
  strength: 'low' | 'medium' | 'strong' | 'high' | 'hard';
  active?: boolean;
  description: string;
  value: Record<string, unknown>;
  effectiveFrom?: string | null;
  effectiveUntil?: string | null;
}

export type RememberedPreferenceUpdate = Partial<RememberedPreferenceWrite> & {
  revision?: number;
  active?: boolean;
};

export async function createRememberedPreference(
  data: RememberedPreferenceWrite,
): Promise<RememberedPreference> {
  return apiRequest<RememberedPreference>(`${BASE}/remembered`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
}

export async function deleteRememberedPreference(
  preferenceId: string,
): Promise<void> {
  await apiRequest<void>(`${BASE}/remembered/${preferenceId}`, {
    method: 'DELETE',
  });
}
