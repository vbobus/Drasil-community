import type { RecurrenceFrequency, TaskRecurrenceRule } from '../types/node';
import { apiRequest } from './http';

const BASE = '/api/v1/recurrence-rules';

export interface RecurrenceRuleWrite {
  frequency: RecurrenceFrequency;
  interval?: number;
  daysOfWeek: string[];
  startDate: string;
  endDate?: string | null;
  preferredStartTime: string;
  durationMinutes: number;
  timezone: string;
  expectedRevision?: number;
}

export async function saveRecurrenceRule(
  taskId: string,
  rule: RecurrenceRuleWrite,
): Promise<TaskRecurrenceRule> {
  return apiRequest<TaskRecurrenceRule>(`${BASE}/${taskId}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(rule),
  });
}

export async function stopRecurrenceRule(
  taskId: string,
  expectedRevision: number,
): Promise<TaskRecurrenceRule> {
  return apiRequest<TaskRecurrenceRule>(
    `${BASE}/${taskId}?expectedRevision=${expectedRevision}`,
    { method: 'DELETE' },
  );
}
