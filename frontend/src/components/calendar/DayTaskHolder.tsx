import { useEffect, useState } from 'react';
import type { DragEvent, CSSProperties } from 'react';
import { Check, ChevronDown, Plus } from 'lucide-react';
import type { StrategyNode } from '../../types/node';
import { useCategoryStore } from '../../store/useCategoryStore';
import { useNodeStore } from '../../store/useNodeStore';
import { categoryFill } from '../../lib/categoryPalette';
import './DayTaskHolder.css';

export function DayTaskHolder({ day, tasks, onAdd, onFocus, onComplete, onAssign, isDropTarget = false }: {
  day: string; tasks: StrategyNode[]; onAdd?: (day: string) => void;
  onFocus: (id: string, categoryId: number) => void;
  onComplete: (id: string, done: boolean) => void;
  onAssign?: (id: string, day: string) => void;
  isDropTarget?: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const [hovered, setHovered] = useState(false);
  const [dropHovered, setDropHovered] = useState(false);
  const [draggingTask, setDraggingTask] = useState(false);
  useEffect(() => {
    const clearDrop = () => setDropHovered(false);
    const finishDrag = () => { clearDrop(); setDraggingTask(false); };
    window.addEventListener('pointerup', clearDrop);
    window.addEventListener('pointercancel', clearDrop);
    window.addEventListener('dragend', finishDrag);
    return () => {
      window.removeEventListener('pointerup', clearDrop);
      window.removeEventListener('pointercancel', clearDrop);
      window.removeEventListener('dragend', finishDrag);
    };
  }, []);
  const categories = useCategoryStore(state => state.categories);
  // Keep the dragged item mounted when leaving a hover-expanded list.
  const open = expanded || hovered || draggingTask;
  const canExpand = tasks.length > 3;
  function drop(event: DragEvent) {
    event.preventDefault(); event.stopPropagation();
    setDropHovered(false);
    try {
      const payload = JSON.parse(event.dataTransfer.getData('application/x-drasil-node') || event.dataTransfer.getData('text/plain'));
      if (typeof payload.id === 'string' || typeof payload.id === 'number') onAssign?.(String(payload.id), day);
    } catch { /* Ignore unrelated drags. */ }
  }
  return <section className={`day-task-holder${open && canExpand ? ' is-expanded' : ''}${dropHovered || isDropTarget ? ' is-drop-target' : ''}${draggingTask ? ' is-dragging-task' : ''}`}
    data-day-holder={day} aria-label={`Tasks without a set time for ${day}`}
    onMouseEnter={() => setHovered(true)} onMouseLeave={() => setHovered(false)}
    onPointerEnter={() => { if (onAssign && useNodeStore.getState().activeDragPayload) setDropHovered(true); }}
    onPointerLeave={() => setDropHovered(false)}
    onPointerUp={() => setDropHovered(false)}
    onKeyDown={event => { if (event.key === 'Escape') { setExpanded(false); setHovered(false); } }}
    onDragOver={event => { if (onAssign && (event.dataTransfer.types.includes('application/x-drasil-node') || event.dataTransfer.types.includes('text/plain'))) { event.preventDefault(); event.stopPropagation(); setDropHovered(true); } }}
    onDragLeave={event => { if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setDropHovered(false); }} onDrop={drop}>
    <div className="day-task-holder-heading">
      <span aria-label={`${tasks.length} tasks`}>{tasks.length || null}</span>
      {onAdd && <button type="button" onClick={() => onAdd(day)} aria-label={`Add task for ${day}`}><Plus size={14} /></button>}
    </div>
    <div className="day-task-holder-body">
      <div className="day-task-holder-list" id={`day-tasks-${day}`}>
        {(open ? tasks : tasks.slice(0, 3)).map(task => {
          const category = categories.find(item => item.id === task.categoryId);
          return <div className="day-task-chip" key={task.id} draggable
            style={{ '--day-task-fill': categoryFill(category?.accentColor ?? '#6366f1') } as CSSProperties}
            onDragStart={event => {
              setDraggingTask(true);
              const data = JSON.stringify({ id: task.id, categoryId: task.categoryId, parentId: task.parentId ?? null, sortOrder: task.sortOrder ?? 0 });
              event.dataTransfer.setData('application/x-drasil-node', data); event.dataTransfer.effectAllowed = 'move';
            }} onDragEnd={() => { setDraggingTask(false); setHovered(false); }}>
            <button type="button" className="day-task-check" onClick={() => onComplete(task.id, true)} aria-label={`Complete ${task.title}`}><Check size={12} /></button>
            <button type="button" className="day-task-title" onClick={() => onFocus(task.id, task.categoryId)} title={task.title}>{task.title}</button>
            {task.timeEstimateMinutes && <span className="day-task-estimate">{task.timeEstimateMinutes}m</span>}
          </div>;
        })}
      </div>
      {canExpand && <button type="button" className="day-task-expand" aria-expanded={open} aria-controls={`day-tasks-${day}`}
        onClick={() => { setExpanded(!expanded); setHovered(false); }}>
        {open ? (expanded ? 'Collapse tasks' : 'Keep expanded') : `Show ${tasks.length - 3} more`}<ChevronDown size={13} />
      </button>}
    </div>
  </section>;
}
