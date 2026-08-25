import { useMemo, useState } from 'react';
import {
  CalendarDays,
  Clock3,
  Globe2,
  RefreshCw,
  Timer,
  Trash2,
  X,
} from 'lucide-react';
import {
  saveRecurrenceRule,
  stopRecurrenceRule,
} from '../../api/recurrenceRules';
import { useNodeStore } from '../../store/useNodeStore';
import type {
  RecurrenceFrequency,
  StrategyNode,
} from '../../types/node';
import './RecurrenceRuleDialog.css';

const WEEKDAYS = [
  ['monday', 'Mon'],
  ['tuesday', 'Tue'],
  ['wednesday', 'Wed'],
  ['thursday', 'Thu'],
  ['friday', 'Fri'],
  ['saturday', 'Sat'],
  ['sunday', 'Sun'],
] as const;

function localDateValue(value = new Date()): string {
  const year = value.getFullYear();
  const month = String(value.getMonth() + 1).padStart(2, '0');
  const day = String(value.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}

function defaultWeekday(startDate: string): string {
  const parsed = new Date(`${startDate}T12:00:00`);
  return WEEKDAYS[Number.isNaN(parsed.getTime()) ? 0 : (parsed.getDay() + 6) % 7][0];
}

function resolvedTimezone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
}

interface RecurrenceRuleDialogProps {
  node: StrategyNode;
  onClose: () => void;
}

export function RecurrenceRuleDialog({ node, onClose }: RecurrenceRuleDialogProps) {
  const existing = node.recurrenceRule ?? null;
  const initialStartDate = existing?.startDate ?? localDateValue();
  const [frequency, setFrequency] = useState<RecurrenceFrequency>(
    existing?.frequency ?? 'weekly',
  );
  const [daysOfWeek, setDaysOfWeek] = useState<string[]>(
    existing?.daysOfWeek.length
      ? existing.daysOfWeek
      : [defaultWeekday(initialStartDate)],
  );
  const [startDate, setStartDate] = useState(initialStartDate);
  const [hasEndDate, setHasEndDate] = useState(Boolean(existing?.endDate));
  const [endDate, setEndDate] = useState(existing?.endDate ?? '');
  const [preferredStartTime, setPreferredStartTime] = useState(
    existing?.preferredStartTime ?? '09:00',
  );
  const [durationMinutes, setDurationMinutes] = useState(
    existing?.durationMinutes ?? node.timeEstimateMinutes ?? 30,
  );
  const [timezone, setTimezone] = useState(existing?.timezone ?? resolvedTimezone());
  const [isSaving, setIsSaving] = useState(false);
  const [isStopping, setIsStopping] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const fetchNodes = useNodeStore((state) => state.fetchNodes);
  const title = existing ? `Edit recurrence for ${node.title}` : `Make ${node.title} recurring`;
  const canSave = useMemo(
    () => (
      Boolean(startDate)
      && Boolean(preferredStartTime)
      && Boolean(timezone.trim())
      && durationMinutes >= 5
      && (frequency === 'daily' || daysOfWeek.length > 0)
      && (!hasEndDate || Boolean(endDate))
    ),
    [
      daysOfWeek.length,
      durationMinutes,
      endDate,
      frequency,
      hasEndDate,
      preferredStartTime,
      startDate,
      timezone,
    ],
  );

  const save = async () => {
    if (!canSave || isSaving) return;
    setIsSaving(true);
    setError(null);
    try {
      await saveRecurrenceRule(node.id, {
        frequency,
        interval: 1,
        daysOfWeek: frequency === 'weekly' ? daysOfWeek : [],
        startDate,
        endDate: hasEndDate ? endDate : null,
        preferredStartTime,
        durationMinutes,
        timezone: timezone.trim(),
        ...(existing ? { expectedRevision: existing.revision } : {}),
      });
      await fetchNodes(node.categoryId);
      onClose();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Unable to save recurrence.');
      setIsSaving(false);
    }
  };

  const stop = async () => {
    if (!existing || isStopping) return;
    setIsStopping(true);
    setError(null);
    try {
      await stopRecurrenceRule(node.id, existing.revision);
      await fetchNodes(node.categoryId);
      onClose();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Unable to stop recurrence.');
      setIsStopping(false);
    }
  };

  return (
    <div
      className="recurrence-dialog-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !isSaving && !isStopping) {
          onClose();
        }
      }}
    >
      <section
        className="recurrence-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby={`recurrence-dialog-title-${node.id}`}
        onKeyDown={(event) => {
          if (event.key === 'Escape' && !isSaving && !isStopping) {
            onClose();
          }
        }}
      >
        <header className="recurrence-dialog-header">
          <div className="recurrence-dialog-heading">
            <span className="recurrence-dialog-heading-icon" aria-hidden="true">
              <RefreshCw size={20} />
            </span>
            <div>
              <h2 id={`recurrence-dialog-title-${node.id}`}>{title}</h2>
              <p>The task stays in its current workspace and keeps the same task type.</p>
            </div>
          </div>
          <button
            type="button"
            className="recurrence-dialog-close"
            onClick={onClose}
            aria-label="Close recurrence settings"
            disabled={isSaving || isStopping}
          >
            <X size={18} />
          </button>
        </header>

        <div className="recurrence-dialog-grid">
          <fieldset className="recurrence-dialog-fieldset">
            <legend><RefreshCw size={15} aria-hidden="true" /> Repeats</legend>
            <div className="recurrence-segmented-control">
              {(['daily', 'weekly'] as const).map((value) => (
                <button
                  key={value}
                  type="button"
                  className={frequency === value ? 'active' : ''}
                  onClick={() => setFrequency(value)}
                  aria-pressed={frequency === value}
                >
                  {value === 'daily' ? 'Daily' : 'Weekly'}
                </button>
              ))}
            </div>
            {frequency === 'weekly' && (
              <div className="recurrence-weekdays" aria-label="Repeat on weekdays">
                {WEEKDAYS.map(([value, label]) => {
                  const selected = daysOfWeek.includes(value);
                  return (
                    <button
                      key={value}
                      type="button"
                      className={selected ? 'selected' : ''}
                      aria-pressed={selected}
                      aria-label={`Repeat on ${value}`}
                      onClick={() => setDaysOfWeek((current) => (
                        selected
                          ? current.filter((day) => day !== value)
                          : [...current, value]
                      ))}
                    >
                      {label}
                    </button>
                  );
                })}
              </div>
            )}
          </fieldset>

          <label className="recurrence-dialog-field">
            <span><CalendarDays size={15} aria-hidden="true" /> Starts</span>
            <input
              type="date"
              value={startDate}
              onChange={(event) => setStartDate(event.target.value)}
            />
          </label>

          <label className="recurrence-dialog-field">
            <span><Clock3 size={15} aria-hidden="true" /> Preferred time</span>
            <input
              type="time"
              value={preferredStartTime}
              onChange={(event) => setPreferredStartTime(event.target.value)}
            />
          </label>

          <label className="recurrence-dialog-field">
            <span><Timer size={15} aria-hidden="true" /> Duration</span>
            <div className="recurrence-duration-input">
              <input
                type="number"
                min={5}
                max={1440}
                step={5}
                value={durationMinutes}
                onChange={(event) => setDurationMinutes(Number(event.target.value))}
              />
              <span>minutes</span>
            </div>
          </label>

          <label className="recurrence-dialog-field">
            <span><Globe2 size={15} aria-hidden="true" /> Timezone</span>
            <input
              type="text"
              value={timezone}
              onChange={(event) => setTimezone(event.target.value)}
              autoComplete="off"
            />
          </label>

          <div className="recurrence-end-date">
            <label>
              <input
                type="checkbox"
                checked={hasEndDate}
                onChange={(event) => setHasEndDate(event.target.checked)}
              />
              End on a specific date
            </label>
            {hasEndDate && (
              <input
                type="date"
                value={endDate}
                min={startDate}
                onChange={(event) => setEndDate(event.target.value)}
                aria-label="Recurrence end date"
              />
            )}
          </div>
        </div>

        <p className="recurrence-dialog-note">
          Editing or stopping this rule affects future generation. Calendar blocks already
          accepted or completed stay in your history.
        </p>
        {error && <p className="recurrence-dialog-error" role="alert">{error}</p>}

        <footer className="recurrence-dialog-actions">
          {existing && (
            <button
              type="button"
              className="recurrence-dialog-stop"
              onClick={() => void stop()}
              disabled={isSaving || isStopping}
            >
              <Trash2 size={16} aria-hidden="true" />
              {isStopping ? 'Stopping…' : 'Stop recurring'}
            </button>
          )}
          <span className="recurrence-dialog-action-spacer" />
          <button
            type="button"
            className="recurrence-dialog-cancel"
            onClick={onClose}
            disabled={isSaving || isStopping}
          >
            Cancel
          </button>
          <button
            type="button"
            className="recurrence-dialog-save"
            onClick={() => void save()}
            disabled={!canSave || isSaving || isStopping}
          >
            <RefreshCw size={16} aria-hidden="true" />
            {isSaving ? 'Saving…' : existing ? 'Save changes' : 'Make recurring'}
          </button>
        </footer>
      </section>
    </div>
  );
}
