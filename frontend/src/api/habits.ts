import { apiRequest } from './http';

export interface HabitDay { date: string; scheduled: boolean; completed: boolean }
export interface Habit {
  id: string; title: string; cue: string; icon: string; weekdays: number[];
  createdOn: string; archived: boolean; currentStreak: number; bestStreak: number;
  totalCheckIns: number; week: HabitDay[];
}
export interface HabitInput { title: string; cue: string; icon: string; weekdays: number[] }
export interface HabitSnapshot {
  today: string; habits: Habit[]; totalXp: number; level: number; levelXp: number;
  totalCheckIns: number; bestStreak: number;
  badges: { id: string; title: string; description: string; earned: boolean }[];
}

export function habitsRequest(path = '', method = 'GET', body?: unknown, signal?: AbortSignal) {
  const timeZone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
  return apiRequest<HabitSnapshot>(`/api/v1/habits${path}?timeZone=${encodeURIComponent(timeZone)}`, {
    method, signal, headers: { 'Content-Type': 'application/json' },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
}
