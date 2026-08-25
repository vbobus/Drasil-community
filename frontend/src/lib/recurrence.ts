import type { TaskRecurrenceRule } from '../types/node';

const WEEKDAY_LABELS: Record<string, string> = {
  monday: 'Mon',
  tuesday: 'Tue',
  wednesday: 'Wed',
  thursday: 'Thu',
  friday: 'Fri',
  saturday: 'Sat',
  sunday: 'Sun',
};

export function formatRecurrenceSummary(rule?: TaskRecurrenceRule | null): string {
  if (!rule) return 'Does not repeat';
  const cadence = rule.frequency === 'daily'
    ? 'Every day'
    : `Every ${rule.daysOfWeek
        .map((day) => WEEKDAY_LABELS[day] ?? day)
        .join(', ')}`;
  return `${cadence} at ${rule.preferredStartTime}`;
}
