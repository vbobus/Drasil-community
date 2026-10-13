import { apiFetch } from './connection';

export interface CompanionSettings {
  enabled: boolean; paired: boolean; time_zone: string; check_in_hours: number[];
  daily_run_limit: number; personal_context: string; worker_status: string;
  memory_status: string; last_seen_at: string | null;
}
export interface CompanionRun {
  id: string; kind: string; prompt: string; status: string; created_at: string;
  error: string | null; dismissed: boolean;
  result: { message: string; suggested_prompt: string | null; quiet: boolean } | null;
}
export async function companionRequest<T>(path: string, method = 'GET', body?: unknown): Promise<T> {
  const response = await apiFetch(`/api/v1/companion/${path}`, {
    method, headers: { 'Content-Type': 'application/json' },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Could not update your companion. Please retry.');
  return data;
}
