import { useCallback, useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { Activity, Archive, Award, BookOpen, Check, ChevronDown, Droplet, Flame, Heart, Moon, Pencil, Plus, RotateCcw, Sprout, Sun, Trophy, X } from 'lucide-react';
import { habitsRequest } from '../../api/habits';
import type { Habit, HabitInput, HabitSnapshot } from '../../api/habits';
import { useMediaQuery } from '../../hooks/useMediaQuery';
import './HabitsPage.css';

const icons = { sprout: Sprout, book: BookOpen, activity: Activity, droplet: Droplet, moon: Moon, heart: Heart, sun: Sun, pencil: Pencil };
const iconLabels = { sprout: 'Growth', book: 'Reading', activity: 'Movement', droplet: 'Water', moon: 'Rest', heart: 'Wellbeing', sun: 'Morning', pencil: 'Writing' };
const weekdayNames = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];
const allDays = [0, 1, 2, 3, 4, 5, 6];
const milestoneLabels: Record<string, string> = { first: 'First check-in', ten: '10 check-ins', streak: '7-check-in streak', fifty: '50 check-ins' };
const starters: HabitInput[] = [
  { title: 'Read 10 pages', cue: 'After my morning coffee', icon: 'book', weekdays: allDays },
  { title: 'Take a 15-minute walk', cue: 'After lunch, before I sit down again', icon: 'activity', weekdays: allDays },
  { title: 'Drink a glass of water', cue: 'When I start my day', icon: 'droplet', weekdays: allDays },
];

function HabitIcon({ name, size = 22 }: { name: string; size?: number }) {
  const Icon = icons[name as keyof typeof icons] || Sprout;
  return <Icon size={size} strokeWidth={1.8} aria-hidden="true" />;
}
function dateFromIso(value: string) { return new Date(`${value}T12:00:00`); }
function scheduleLabel(days: number[]) {
  return days.length === 7 ? 'Every day' : days.map(day => weekdayNames[day].slice(0, 3)).join(' · ');
}

function HabitEditor({ habit, isEditing, compact, busy, error, onSave, onCancel }: {
  habit: HabitInput; isEditing: boolean; compact: boolean; busy: boolean; error: string;
  onSave: (value: HabitInput) => void; onCancel: () => void;
}) {
  const [value, setValue] = useState<HabitInput>(() => ({ title: habit.title, cue: habit.cue, icon: habit.icon, weekdays: habit.weekdays }));
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const element = dialog.current;
    if (!compact || !element) return;
    element.showModal();
    element.querySelector<HTMLInputElement>('input')?.focus({ preventScroll: true });
    return () => element.close();
  }, [compact]);
  const submit = (event: FormEvent) => { event.preventDefault(); onSave({ ...value, title: value.title.trim(), cue: value.cue.trim() }); };
  const form = <form className="habit-editor" onSubmit={submit} aria-label="Habit editor" aria-busy={busy}>
    <div className="habit-section-heading habit-editor-heading"><h2>{isEditing ? 'Edit habit' : 'New habit'}</h2>
      <button type="button" className="habit-icon-button" aria-label="Close habit editor" onClick={onCancel} disabled={busy}><X size={18} /></button></div>
    <fieldset className="habit-editor-body" disabled={busy}>
      <div className="habit-editor-fields">
        <label>Habit name<input autoFocus required maxLength={120} placeholder="e.g. Stretch for five minutes" value={value.title}
          onChange={event => setValue(current => ({ ...current, title: event.target.value }))} /></label>
        <label>When will you do it? <span>(optional)</span><input maxLength={240} placeholder="e.g. After I brush my teeth" value={value.cue}
          onChange={event => setValue(current => ({ ...current, cue: event.target.value }))} /></label>
      </div>
      <div className="habit-editor-options">
        <fieldset><legend>Repeat on</legend><div className="habit-day-picker">
          {weekdayNames.map((name, day) => <button key={name} type="button" aria-label={name} aria-pressed={value.weekdays.includes(day)}
            onClick={() => setValue(current => ({ ...current, weekdays: current.weekdays.includes(day)
              ? current.weekdays.filter(item => item !== day) : [...current.weekdays, day].sort() }))}>{name.slice(0, 3)}</button>)}
        </div></fieldset>
        <fieldset><legend>Icon</legend><div className="habit-icon-picker">
          {Object.keys(icons).map(icon => <button type="button" key={icon} title={iconLabels[icon as keyof typeof icons]}
            aria-label={iconLabels[icon as keyof typeof icons]} aria-pressed={value.icon === icon}
            onClick={() => setValue(current => ({ ...current, icon }))}><HabitIcon name={icon} size={18} /></button>)}
        </div></fieldset>
      </div>
    </fieldset>
    <div className="habit-editor-footer">{error && <p className="habit-editor-error" role="alert">{error}</p>}
      {!value.weekdays.length ? <p>Choose at least one day.</p> : isEditing && <p>Schedule changes start today.</p>}
      <button type="button" className="habit-button" onClick={onCancel} disabled={busy}>Cancel</button>
      <button className="habit-button habit-button-primary" disabled={busy || !value.title.trim() || !value.weekdays.length}>{busy ? 'Saving…' : 'Save habit'}</button></div>
  </form>;
  return compact ? <dialog ref={dialog} className="habit-editor-dialog" aria-label={isEditing ? 'Edit habit' : 'New habit'}
    onCancel={event => { event.preventDefault(); if (!busy) onCancel(); }}>{form}</dialog> : form;
}

export function HabitsPage() {
  const [data, setData] = useState<HabitSnapshot | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [pendingCheckIn, setPendingCheckIn] = useState<{ habitId: string; day: string } | null>(null);
  const [loading, setLoading] = useState(true);
  const [editor, setEditor] = useState<{ id?: string; value: HabitInput } | null>(null);
  const [notice, setNotice] = useState('');
  const [celebrating, setCelebrating] = useState(false);
  const [showArchived, setShowArchived] = useState(false);
  const sequence = useRef(0);
  const saving = useRef(false);
  const addButton = useRef<HTMLButtonElement>(null);
  const editorTrigger = useRef<HTMLButtonElement | null>(null);
  const compact = useMediaQuery('(max-width: 1023px)');

  const load = useCallback(async (signal?: AbortSignal) => {
    if (saving.current) return;
    const request = ++sequence.current;
    try {
      const result = await habitsRequest('', 'GET', undefined, signal);
      if (request === sequence.current) { setData(result); setError(''); }
    } catch (failure) {
      if (!signal?.aborted && request === sequence.current) setError(failure instanceof Error ? failure.message : 'Could not load habits. Try again.');
    } finally { if (request === sequence.current && !signal?.aborted) setLoading(false); }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    const initial = window.setTimeout(() => void load(controller.signal), 0);
    const refresh = () => { if (document.visibilityState === 'visible') void load(controller.signal); };
    const timer = window.setInterval(refresh, 60_000);
    window.addEventListener('focus', refresh);
    return () => { controller.abort(); sequence.current += 1; window.clearTimeout(initial); window.clearInterval(timer); window.removeEventListener('focus', refresh); };
  }, [load]);

  async function mutate(path: string, method: string, body: unknown, message: string, celebrate = false) {
    if (saving.current) return false;
    saving.current = true;
    const request = ++sequence.current;
    setBusy(true); setError(''); setNotice(''); setCelebrating(false);
    try {
      const result = await habitsRequest(path, method, body);
      if (request === sequence.current) { setData(result); setNotice(message); setCelebrating(celebrate); }
      return true;
    } catch (failure) {
      if (request === sequence.current) setError(failure instanceof Error ? failure.message : 'Could not save your change. Try again.');
      return false;
    } finally { saving.current = false; if (request === sequence.current) setBusy(false); }
  }
  function openEditor(next: { id?: string; value: HabitInput }, trigger: HTMLButtonElement) {
    editorTrigger.current = trigger;
    setEditor(next);
  }
  function closeEditor() {
    setEditor(null);
    window.requestAnimationFrame(() => {
      const trigger = editorTrigger.current;
      (trigger?.isConnected ? trigger : addButton.current)?.focus({ preventScroll: true });
    });
  }
  async function saveHabit(value: HabitInput) {
    if (await mutate(editor?.id ? `/${editor.id}` : '', editor?.id ? 'PUT' : 'POST', value, editor?.id ? 'Habit updated.' : 'Habit added.')) closeEditor();
  }
  async function toggleCheckIn(habit: Habit, day: string, completed: boolean) {
    if (saving.current) return;
    setPendingCheckIn({ habitId: habit.id, day });
    try {
      await mutate(`/${habit.id}/check-ins/${day}`, 'PUT', { completed: !completed }, completed
        ? `Check-in undone for ${habit.title}.` : `${habit.title} complete. +10 XP.`, !completed);
    } finally { setPendingCheckIn(null); }
  }

  const active = data?.habits.filter(habit => !habit.archived) ?? [];
  const archived = data?.habits.filter(habit => habit.archived) ?? [];
  const today = data?.today ?? '';
  const due = active.filter(habit => habit.week.at(-1)?.scheduled || habit.week.at(-1)?.completed);
  const done = due.filter(habit => habit.week.at(-1)?.completed).length;
  const allComplete = due.length > 0 && done === due.length;

  return <section className="habits-page" aria-labelledby="habits-heading">
    <div className="habits-content">
      <header className="habits-header"><h1 id="habits-heading">Habits</h1>
        <button ref={addButton} type="button" className="habit-button habit-button-primary" onClick={event => openEditor({ value: { title: '', cue: '', icon: 'sprout', weekdays: allDays } }, event.currentTarget)} disabled={busy}>
          <Plus size={18} aria-hidden="true" /> New habit</button></header>
      {error && !(editor && compact) && <div className="habit-error" role="alert"><span>{error}</span><button className="habit-button" type="button" onClick={() => void load()} disabled={busy}>Retry loading</button></div>}
      {editor && <HabitEditor key={editor.id ?? editor.value.title} habit={editor.value} isEditing={!!editor.id} compact={compact} busy={busy} error={compact ? error : ''} onSave={value => void saveHabit(value)} onCancel={closeEditor} />}
      {loading && !data ? <div className="habits-loading" role="status">Loading your habits…</div> : data ? <>
        <div className="habits-layout">
          <div className="habits-main-column">
            {active.length > 0 && <section className={`habit-today${allComplete ? ' is-complete' : ''}`} aria-labelledby="habit-today-heading">
              <div className="habit-section-heading"><h2 id="habit-today-heading">{allComplete ? 'Today complete' : 'Today'}</h2>
                <span className="habit-date">{dateFromIso(today).toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' })}</span></div>
              {due.length ? <div className="habit-daily-progress"><progress aria-label="Today's habit progress" max={due.length} value={done} /><strong>{done} / {due.length} complete</strong></div>
                : <p>No habits scheduled today.</p>}
            </section>}
            <div className={`habit-notice${celebrating ? ' is-celebrating' : ''}`} role="status" aria-live="polite" aria-atomic="true">
              {notice && <><Check size={16} aria-hidden="true" /><span>{notice}</span>{celebrating && <span className="habit-reward-sparks" aria-hidden="true"><i /><i /><i /></span>}</>}
            </div>
            {active.length ? <section aria-labelledby="your-habits-heading">
              <div className="habit-section-heading habit-list-heading"><h2 id="your-habits-heading">Your habits</h2><span>Last 7 days · tap a day to check in</span></div>
              <div className="habit-list">
                {active.map(habit => {
                  const current = habit.week.at(-1)!;
                  const pendingToday = pendingCheckIn?.habitId === habit.id && pendingCheckIn.day === today;
                  return <article key={habit.id} className={`habit-row${current.completed ? ' is-done' : ''}`} aria-label={habit.title}>
                    <div className="habit-row-top"><span className={`habit-symbol habit-symbol-${habit.icon}`}><HabitIcon name={habit.icon} /></span>
                      <div className="habit-identity"><h3>{habit.title}</h3>{habit.cue && <p>{habit.cue}</p>}</div>
                      <button type="button" className={`habit-button habit-check-in${current.completed ? ' is-done' : ''}`} disabled={busy || (!current.scheduled && !current.completed)}
                        aria-label={pendingToday ? `Saving ${habit.title}` : current.completed ? `Undo ${habit.title} today` : `Check in ${habit.title}`} aria-pressed={current.completed} aria-busy={pendingToday}
                        onClick={() => void toggleCheckIn(habit, today, current.completed)}>
                        {pendingToday ? 'Saving…' : current.completed ? <><Check size={18} aria-hidden="true" /> Done <span>Undo</span></> : current.scheduled ? <><Plus size={18} aria-hidden="true" /> Check in <span>+10 XP</span></> : 'Rest day'}
                      </button></div>
                    <div className="habit-row-bottom"><div className="habit-row-meta"><span className="habit-streak"><Flame size={16} aria-hidden="true" /><strong>{habit.currentStreak}</strong> check-in streak</span>
                      <span>{habit.totalCheckIns} total · {scheduleLabel(habit.weekdays)}</span>
                      <div className="habit-row-tools"><button type="button" className="habit-icon-button" title="Edit habit" aria-label={`Edit ${habit.title}`} disabled={busy} onClick={event => openEditor({ id: habit.id, value: habit }, event.currentTarget)}><Pencil size={15} /></button>
                        <button type="button" className="habit-icon-button" title="Archive habit" aria-label={`Archive ${habit.title}`} disabled={busy}
                          onClick={() => void mutate(`/${habit.id}/archive`, 'PATCH', { archived: true }, `${habit.title} archived. History and XP saved.`)}><Archive size={15} /></button></div></div>
                      <div className="habit-week" aria-label={`${habit.title} last seven days`}>
                        {habit.week.map(day => <button key={day.date} type="button" className={`${day.completed ? 'is-done' : ''}${day.date === today ? ' is-today' : ''}`}
                          disabled={busy || (!day.scheduled && !day.completed)} aria-pressed={day.completed} aria-busy={pendingCheckIn?.habitId === habit.id && pendingCheckIn.day === day.date}
                          aria-label={`${day.completed ? 'Undo' : 'Check in'} ${habit.title} on ${day.date}${!day.scheduled && !day.completed ? ' (not scheduled)' : ''}`}
                          title={`${dateFromIso(day.date).toLocaleDateString(undefined, { weekday: 'long', month: 'short', day: 'numeric' })} · ${day.completed ? 'Complete' : day.scheduled ? 'Check in' : 'Not scheduled'}`}
                          onClick={() => void toggleCheckIn(habit, day.date, day.completed)}><span>{dateFromIso(day.date).toLocaleDateString(undefined, { weekday: 'narrow' })}</span>
                          <span className="habit-day-mark">{pendingCheckIn?.habitId === habit.id && pendingCheckIn.day === day.date ? '…' : day.completed ? <Check size={15} aria-hidden="true" /> : dateFromIso(day.date).getDate()}</span></button>)}
                      </div></div>
                  </article>;
                })}
              </div>
            </section> : <section className="habits-empty" aria-labelledby="habit-empty-heading"><Sprout size={34} strokeWidth={1.5} aria-hidden="true" />
              <h2 id="habit-empty-heading">{archived.length ? 'No active habits' : 'No habits yet'}</h2><p>Add a habit or choose a starter.</p>
              <div className="habit-starters">{starters.map(starter => <button key={starter.title} type="button" className="habit-starter" disabled={busy} onClick={event => openEditor({ value: starter }, event.currentTarget)}>
                <HabitIcon name={starter.icon} size={20} /><span>{starter.title}</span><Plus size={16} aria-hidden="true" /></button>)}</div></section>}
            {archived.length > 0 && <section className="habit-archive"><button type="button" className="habit-archive-toggle" aria-expanded={showArchived} onClick={() => setShowArchived(value => !value)}>Archived habits ({archived.length})</button>
              {showArchived && archived.map(habit => <div key={habit.id} className="habit-archived-row"><HabitIcon name={habit.icon} size={18} /><span>{habit.title}<small>{habit.totalCheckIns} check-ins saved</small></span>
                <button type="button" className="habit-button" disabled={busy} onClick={() => void mutate(`/${habit.id}/archive`, 'PATCH', { archived: false }, `${habit.title} restored.`)}><RotateCcw size={14} /> Restore</button></div>)}</section>}
          </div>
          <aside className="habit-rewards" aria-label="Habit rewards">
            <section className="habit-level" aria-labelledby="habit-level-heading"><span className="habit-level-emblem"><Sprout size={30} strokeWidth={1.6} aria-hidden="true" /></span>
              <h2 id="habit-level-heading">Level {data.level}</h2>
              <div className="habit-xp-heading"><strong>{data.totalXp} XP</strong><span>{100 - data.levelXp} to level {data.level + 1}</span></div>
              <progress aria-label="XP toward next level" max={100} value={data.levelXp} />
            </section>
            <details className="habit-milestones" open={!compact} aria-labelledby="habit-milestone-heading"><summary><h2 id="habit-milestone-heading"><Trophy size={18} aria-hidden="true" /> Milestones</h2>
              <span>{data.badges.filter(badge => badge.earned).length} / {data.badges.length}</span><ChevronDown size={18} aria-hidden="true" /></summary>
              <ul>{data.badges.map(badge => <li key={badge.id} className={badge.earned ? 'is-earned' : ''}><Award size={24} strokeWidth={1.6} aria-hidden="true" />
                <h3>{milestoneLabels[badge.id] ?? badge.title}{badge.earned && <Check size={13} aria-label="Earned" />}</h3></li>)}</ul></details>
          </aside>
        </div>
      </> : !error ? <p>No habits to show.</p> : null}
    </div>
  </section>;
}
