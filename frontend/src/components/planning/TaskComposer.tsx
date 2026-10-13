import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { CalendarDays, Clock3, Flag, Hash, X } from 'lucide-react';
import { useNodeStore } from '../../store/useNodeStore';
import type { Category } from '../../types/category';
import type { NodePriority } from '../../types/node';
import { localDayKey } from '../../lib/weekPlanning';
import './TaskComposer.css';

export function TaskComposer({ categories, categoryId, initialDay, onClose }: {
  categories: Category[]; categoryId: number; initialDay?: string; onClose: () => void;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const titleRef = useRef<HTMLTextAreaElement>(null);
  const [title, setTitle] = useState('');
  const [notes, setNotes] = useState('');
  const [day, setDay] = useState(initialDay ?? localDayKey(new Date()));
  const [category, setCategory] = useState(categoryId);
  const [duration, setDuration] = useState('');
  const [priority, setPriority] = useState<NodePriority>('medium');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const addNode = useNodeStore(state => state.addNode);

  useEffect(() => {
    const trigger = document.activeElement;
    dialogRef.current?.showModal();
    titleRef.current?.focus();
    return () => { if (trigger instanceof HTMLElement) trigger.focus(); };
  }, []);

  async function save() {
    if (!title.trim() || saving) return;
    setSaving(true); setError('');
    try {
      await addNode({ title: title.trim(), notes: notes.trim(), categoryId: category,
        plannedDate: day || null, priority, nodeType: 'task',
        timeEstimateMinutes: duration ? Number(duration) : null });
      onClose();
    } catch {
      setError('Could not add this task. Your details are still here — try again.');
      setSaving(false);
    }
  }

  return createPortal(
    <dialog ref={dialogRef} className="task-composer" aria-labelledby="task-composer-title"
      onCancel={event => { event.preventDefault(); if (!saving) onClose(); }}>
      <form onSubmit={event => { event.preventDefault(); void save(); }}
        onKeyDown={event => {
          if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
            event.preventDefault(); event.currentTarget.requestSubmit();
          }
        }}>
        <header className="task-composer-header">
          <h2 id="task-composer-title">Add a task</h2>
          <button type="button" onClick={onClose} disabled={saving} aria-label="Close task composer"><X size={20} /></button>
        </header>
        <textarea ref={titleRef} className="task-composer-title" aria-label="Task description" placeholder="What would you like to do?"
          autoFocus required maxLength={256} rows={2} value={title} onChange={event => setTitle(event.target.value)} />
        <textarea className="task-composer-notes" aria-label="Task notes" placeholder="Add a note or paste a link…"
          maxLength={4000} rows={2} value={notes} onChange={event => setNotes(event.target.value)} />
        <div className="task-composer-fields">
          <label><span><CalendarDays size={15} /> Planned day</span><input type="date" value={day} onChange={event => setDay(event.target.value)} /></label>
          <label><span><Clock3 size={15} /> Estimate</span><input type="number" min={1} max={10080} placeholder="Minutes" value={duration} onChange={event => setDuration(event.target.value)} /></label>
          <label><span><Hash size={15} /> Workspace</span><select value={category} onChange={event => setCategory(Number(event.target.value))}>
            {categories.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
          </select></label>
          <label><span><Flag size={15} /> Priority</span><select value={priority} onChange={event => setPriority(event.target.value as NodePriority)}>
            <option value="low">Low</option><option value="medium">Normal</option><option value="high">High</option>
          </select></label>
        </div>
        {error && <p className="task-composer-error" role="alert">{error}</p>}
        <footer className="task-composer-footer">
          <span>{day ? 'On your day, without a set time.' : 'In your task tree, without a planned day.'}</span>
          <button className="task-composer-submit" type="submit" disabled={!title.trim() || saving}>{saving ? 'Adding…' : 'Add task'}</button>
        </footer>
      </form>
    </dialog>, document.body,
  );
}
