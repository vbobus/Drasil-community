import { useEffect, useMemo, useState } from 'react';
import type { CSSProperties } from 'react';
import {
  ArrowUpRight,
  CalendarDays,
  Check,
  ChevronLeft,
  ChevronRight,
  Clock3,
  ListChecks,
} from 'lucide-react';
import { useCalendarStore } from '../../store/useCalendarStore';
import { suggestEmoji } from '../../lib/emoji';
import type { Category } from '../../types/category';
import type { CalendarBlock } from '../../types/calendar';
import type { StrategyNode } from '../../types/node';
import './MyDayAgenda.css';

interface AgendaFocusTask {
  node: StrategyNode;
  category: Category | null;
}

interface MyDayAgendaProps {
  taskLookup: Map<string, StrategyNode>;
  focusTasks: AgendaFocusTask[];
  onTaskStatusChange: (taskId: string, done: boolean) => boolean | void | Promise<boolean | void>;
  onTaskFocus: (taskId: string, categoryId: number) => void;
  onOpenCalendar: () => void;
}

function startOfLocalDay(date: Date) {
  const nextDate = new Date(date);
  nextDate.setHours(0, 0, 0, 0);
  return nextDate;
}

function addDays(date: Date, days: number) {
  const nextDate = new Date(date);
  nextDate.setDate(nextDate.getDate() + days);
  return nextDate;
}

function toLocalIsoMinute(date: Date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  const hours = String(date.getHours()).padStart(2, '0');
  const minutes = String(date.getMinutes()).padStart(2, '0');
  return `${year}-${month}-${day}T${hours}:${minutes}:00`;
}

function formatDuration(minutes: number) {
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const remainder = minutes % 60;
  return remainder ? `${hours}h ${remainder}m` : `${hours}h`;
}

const timeFormatter = new Intl.DateTimeFormat('en-US', {
  hour: 'numeric',
  minute: '2-digit',
});
const agendaDateFormatter = new Intl.DateTimeFormat('en-US', {
  weekday: 'long',
  month: 'long',
  day: 'numeric',
});

function formatAgendaTime(block: CalendarBlock, dayStart: Date, dayEnd: Date) {
  const start = new Date(block.startAt);
  const end = new Date(block.endAt);
  const startsBeforeDay = start < dayStart;
  const endsAfterDay = end > dayEnd;

  if (startsBeforeDay && endsAfterDay) return 'All day';
  if (startsBeforeDay) return `Until ${timeFormatter.format(end)}`;
  if (endsAfterDay) return `${timeFormatter.format(start)} onward`;
  return `${timeFormatter.format(start)} – ${timeFormatter.format(end)}`;
}

function isDone(block: CalendarBlock, task: StrategyNode | undefined) {
  return task?.status === 'done'
    || block.taskStatus === 'done'
    || block.completionStatus === 'completed';
}

function blockStatus(block: CalendarBlock, selectedDate: Date, now: Date) {
  const selectedDay = startOfLocalDay(selectedDate);
  const today = startOfLocalDay(now);
  if (selectedDay.getTime() !== today.getTime()) return null;

  const start = new Date(block.startAt);
  const end = new Date(block.endAt);
  if (start <= now && end > now) return 'Now';
  if (start > now) return 'Upcoming';
  return 'Finished';
}

function agendaDayLabel(date: Date, today: Date) {
  const selectedDay = startOfLocalDay(date);
  const currentDay = startOfLocalDay(today);
  const difference = Math.round(
    (selectedDay.getTime() - currentDay.getTime()) / (24 * 60 * 60 * 1000),
  );
  if (difference === 0) return 'Today';
  if (difference === 1) return 'Tomorrow';
  if (difference === -1) return 'Yesterday';
  return new Intl.DateTimeFormat('en-US', { weekday: 'long' }).format(date);
}

export function MyDayAgenda({
  taskLookup,
  focusTasks,
  onTaskStatusChange,
  onTaskFocus,
  onOpenCalendar,
}: MyDayAgendaProps) {
  const [selectedDate, setSelectedDate] = useState(() => startOfLocalDay(new Date()));
  const [now, setNow] = useState(() => new Date());
  const [optimisticTaskStatuses, setOptimisticTaskStatuses] = useState<Record<string, boolean>>({});
  const [pendingTaskIds, setPendingTaskIds] = useState<Set<string>>(() => new Set());
  const blocks = useCalendarStore((state) => state.blocks);
  const isLoading = useCalendarStore((state) => state.isLoading);
  const error = useCalendarStore((state) => state.error);
  const fetchBlocks = useCalendarStore((state) => state.fetchBlocks);
  const dayStart = useMemo(() => startOfLocalDay(selectedDate), [selectedDate]);
  const dayEnd = useMemo(() => addDays(dayStart, 1), [dayStart]);

  useEffect(() => {
    void fetchBlocks(toLocalIsoMinute(dayStart), toLocalIsoMinute(dayEnd), { force: false });
  }, [dayEnd, dayStart, fetchBlocks]);

  useEffect(() => {
    const intervalId = window.setInterval(() => setNow(new Date()), 60_000);
    return () => window.clearInterval(intervalId);
  }, []);

  const agendaBlocks = useMemo(
    () => blocks
      .filter((block) => block.variant === 'scheduled' || block.variant === 'protected')
      .filter((block) => new Date(block.startAt) < dayEnd && new Date(block.endAt) > dayStart)
      .sort((first, second) => Date.parse(first.startAt) - Date.parse(second.startAt)),
    [blocks, dayEnd, dayStart],
  );
  const scheduledTaskIds = useMemo(
    () => new Set(agendaBlocks.map((block) => block.taskId)),
    [agendaBlocks],
  );
  const unscheduledFocusTasks = useMemo(
    () => focusTasks.filter(({ node }) => !scheduledTaskIds.has(node.id)),
    [focusTasks, scheduledTaskIds],
  );
  const completedCount = agendaBlocks.filter((block) =>
    optimisticTaskStatuses[block.taskId] ?? isDone(block, taskLookup.get(block.taskId)),
  ).length;
  const scheduledMinutes = agendaBlocks.reduce((total, block) => {
    const overlapStart = Math.max(dayStart.getTime(), Date.parse(block.startAt));
    const overlapEnd = Math.min(dayEnd.getTime(), Date.parse(block.endAt));
    return total + Math.max(0, Math.round((overlapEnd - overlapStart) / 60000));
  }, 0);
  const progress = agendaBlocks.length > 0
    ? Math.round((completedCount / agendaBlocks.length) * 100)
    : 0;
  const isToday = dayStart.getTime() === startOfLocalDay(now).getTime();

  const handleCompletionChange = async (block: CalendarBlock, done: boolean) => {
    const previousStatus = optimisticTaskStatuses[block.taskId];
    setOptimisticTaskStatuses((current) => ({ ...current, [block.taskId]: done }));
    setPendingTaskIds((current) => new Set(current).add(block.taskId));
    let succeeded: boolean;
    try {
      succeeded = await Promise.resolve(onTaskStatusChange(block.taskId, done)) !== false;
    } catch {
      succeeded = false;
    }
    if (succeeded === false) {
      setOptimisticTaskStatuses((current) => {
        const next = { ...current };
        if (previousStatus === undefined) {
          delete next[block.taskId];
        } else {
          next[block.taskId] = previousStatus;
        }
        return next;
      });
    }
    setPendingTaskIds((current) => {
      const next = new Set(current);
      next.delete(block.taskId);
      return next;
    });
  };

  return (
    <section className="my-day-agenda" aria-labelledby="my-day-heading">
      <header className="my-day-header">
        <div>
          <p className="my-day-eyebrow">{agendaDayLabel(selectedDate, now)}</p>
          <h1 id="my-day-heading">{agendaDateFormatter.format(selectedDate)}</h1>
          <p className="my-day-subtitle">Your commitments and focus for the day, in one place.</p>
        </div>

        <div className="my-day-header-actions">
          <div className="my-day-date-nav" aria-label="Agenda date navigation">
            <button
              type="button"
              onClick={() => setSelectedDate((current) => addDays(current, -1))}
              aria-label="Previous day"
            >
              <ChevronLeft size={17} />
            </button>
            <button
              type="button"
              className="my-day-today-button"
              onClick={() => setSelectedDate(startOfLocalDay(new Date()))}
              disabled={isToday}
            >
              Today
            </button>
            <button
              type="button"
              onClick={() => setSelectedDate((current) => addDays(current, 1))}
              aria-label="Next day"
            >
              <ChevronRight size={17} />
            </button>
          </div>
          <button type="button" className="my-day-calendar-button" onClick={onOpenCalendar}>
            <CalendarDays size={16} />
            Open calendar
          </button>
        </div>
      </header>

      <div className="my-day-overview" aria-label="Daily overview">
        <div className="my-day-progress" style={{ '--agenda-progress': `${progress}%` } as CSSProperties}>
          <span>{progress}%</span>
        </div>
        <div className="my-day-overview-copy">
          <strong>{completedCount} of {agendaBlocks.length} complete</strong>
          <span>{agendaBlocks.length === 0 ? 'A clear day so far' : 'Keep the day moving'}</span>
        </div>
        <div className="my-day-metric">
          <Clock3 size={16} />
          <span><strong>{formatDuration(scheduledMinutes)}</strong> scheduled</span>
        </div>
        <div className="my-day-metric">
          <ListChecks size={16} />
          <span><strong>{unscheduledFocusTasks.length}</strong> in focus list</span>
        </div>
      </div>

      <div className="my-day-content">
        <section className="my-day-agenda-column" aria-labelledby="agenda-list-heading">
          <div className="my-day-section-heading">
            <div>
              <p>Schedule</p>
              <h2 id="agenda-list-heading">Agenda</h2>
            </div>
            <span>{agendaBlocks.length} {agendaBlocks.length === 1 ? 'item' : 'items'}</span>
          </div>

          {isLoading ? (
            <div className="my-day-state" aria-live="polite">Loading your agenda…</div>
          ) : error ? (
            <div className="my-day-state is-error" role="alert">{error}</div>
          ) : agendaBlocks.length === 0 ? (
            <div className="my-day-empty">
              <CalendarDays size={22} />
              <h3>Nothing scheduled</h3>
              <p>Your day is open. Use the calendar or AI workspace when you are ready to plan it.</p>
              <button type="button" onClick={onOpenCalendar}>Open calendar</button>
            </div>
          ) : (
            <ol className="my-day-timeline">
              {agendaBlocks.map((block) => {
                const task = taskLookup.get(block.taskId);
                const done = optimisticTaskStatuses[block.taskId] ?? isDone(block, task);
                const isPending = pendingTaskIds.has(block.taskId);
                const status = blockStatus(block, selectedDate, now);
                const emoji = task?.emoji || block.emoji || suggestEmoji(block.title, task?.type ?? 'task');
                return (
                  <li key={block.id} className={done ? 'is-complete' : ''}>
                    <time>{formatAgendaTime(block, dayStart, dayEnd)}</time>
                    <span className="my-day-timeline-node" aria-hidden="true" />
                    <article
                      className="my-day-agenda-card"
                      style={{ '--agenda-color': block.categoryColor } as CSSProperties}
                    >
                      <button
                        type="button"
                        className="my-day-complete-button"
                        aria-label={`${done ? 'Mark incomplete' : 'Mark complete'}: ${block.title}`}
                        aria-pressed={done}
                        aria-busy={isPending}
                        disabled={isPending}
                        onClick={() => void handleCompletionChange(block, !done)}
                      >
                        {done && <Check size={13} />}
                      </button>
                      <button
                        type="button"
                        className="my-day-agenda-card-main"
                        onClick={() => onTaskFocus(block.taskId, block.categoryId)}
                      >
                        <span className="my-day-task-emoji" aria-hidden="true">{emoji}</span>
                        <span className="my-day-task-copy">
                          <strong>{block.title}</strong>
                          <span>{block.categoryName}</span>
                        </span>
                        {status && <span className={`my-day-status is-${status.toLowerCase()}`}>{status}</span>}
                        {block.isRecurring && <span className="my-day-recurring">Repeats</span>}
                        <ArrowUpRight size={15} aria-hidden="true" />
                      </button>
                    </article>
                  </li>
                );
              })}
            </ol>
          )}
        </section>

        <aside className="my-day-focus-column" aria-labelledby="focus-list-heading">
          <div className="my-day-section-heading">
            <div>
              <p>Selected tasks</p>
              <h2 id="focus-list-heading">Focus list</h2>
            </div>
            <span>{unscheduledFocusTasks.length}</span>
          </div>
          {unscheduledFocusTasks.length === 0 ? (
            <div className="my-day-focus-empty">
              {focusTasks.length > 0
                ? 'Everything selected is already on this agenda.'
                : 'Select tasks from a workspace to keep them close here.'}
            </div>
          ) : (
            <ul className="my-day-focus-list">
              {unscheduledFocusTasks.map(({ node, category }) => (
                <li key={node.id}>
                  <button
                    type="button"
                    onClick={() => onTaskFocus(node.id, node.categoryId)}
                  >
                    <span
                      className="my-day-focus-emoji"
                      style={{ '--agenda-color': category?.accentColor ?? '#64748b' } as CSSProperties}
                      aria-hidden="true"
                    >
                      {node.emoji || suggestEmoji(node.title, node.type)}
                    </span>
                    <span>
                      <strong>{node.title}</strong>
                      <small>{category?.name ?? 'Workspace'} · Not scheduled</small>
                    </span>
                    <ArrowUpRight size={14} aria-hidden="true" />
                  </button>
                </li>
              ))}
            </ul>
          )}
        </aside>
      </div>
    </section>
  );
}
