import { useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import type { CSSProperties, DragEvent } from 'react';
import { CalendarDays, ChevronLeft, ChevronRight, Clock3, MoreHorizontal, Plus, Trash2 } from 'lucide-react';
import type { Category } from '../../types/category';
import type { StrategyNode } from '../../types/node';
import { useNodeStore } from '../../store/useNodeStore';
import { useCalendarStore } from '../../store/useCalendarStore';
import { useWorkspaceStore } from '../../store/useWorkspaceStore';
import { moveTaskDay } from '../../api/calendarBlocks';
import type { CalendarBlock } from '../../types/calendar';
import { categoryAccent } from '../../lib/categoryPalette';
import { WorkspaceIcon } from '../../lib/workspaceIcons';
import { cardSubtasks, localDayDate, localDayKey, mondayOf, planningCardsByDay, planningCardComplete, planningDueIds, planningDuration, planningEstimate, planningHasDueWork, shiftDay } from '../../lib/weekPlanning';
import './WeeklyPlanningBoard.css';

interface WeeklyPlanningBoardProps {
  nodes: StrategyNode[];
  categoryById: ReadonlyMap<number, Category>;
  isLoading: boolean;
  focusedDay: string | null;
  onFocusDay: (day: string | null) => void;
  onAddTask: (day?: string) => void;
  onOpenTask: (id: string) => void;
  onStatusChange: (id: string, done: boolean, scopeIds?: string[]) => void;
}

// Focus the part of the board that remains visible between the anchored panels.
function visibleBoardBounds(scroll: HTMLElement) {
  const rect = scroll.getBoundingClientRect();
  const shell = scroll.closest('.app-shell-floating');
  const tree = shell?.querySelector<HTMLElement>('.floating-task-repository:not([hidden])')?.getBoundingClientRect();
  const calendar = shell?.querySelector<HTMLElement>('.panel-calendar:not(.is-collapsed)')?.getBoundingClientRect();
  const left = tree && tree.width > 0 ? Math.max(rect.left, tree.right) : rect.left;
  const right = calendar && calendar.width > 0 ? Math.min(rect.right, calendar.left) : rect.right;
  return right > left ? { left, right, center: (left + right) / 2 } : { left: rect.left, right: rect.right, center: rect.left + rect.width / 2 };
}

function TaskCard({ node, day, category, blocks, scheduledDays, onOpenTask, onStatusChange, onDragChange }: {
  node: StrategyNode;
  day: string;
  category?: Category;
  blocks: CalendarBlock[];
  scheduledDays: ReadonlyMap<string, string[]>;
  onOpenTask: (id: string) => void;
  onStatusChange: (id: string, done: boolean, scopeIds?: string[]) => void;
  onDragChange: (dragging: boolean) => void;
}) {
  const subtasks = cardSubtasks(node, day, scheduledDays);
  const isComplete = planningCardComplete(node, day, scheduledDays);
  const previewRef = useRef<HTMLElement | null>(null);
  const cardRef = useRef<HTMLElement | null>(null);
  const dragAllowed = useRef(true);
  const [menuPosition, setMenuPosition] = useState<{ x: number; y: number } | null>(null);
  const [isDeleting, setIsDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const deleteDialog = useRef<HTMLDialogElement>(null);
  const deleteTitleId = useId();
  useEffect(() => {
    if (!menuPosition) return;
    menuRef.current?.querySelector<HTMLButtonElement>('button')?.focus();
    const closeOutside = (event: PointerEvent) => {
      if (!menuRef.current?.contains(event.target as Node)) setMenuPosition(null);
    };
    const close = () => setMenuPosition(null);
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { close(); cardRef.current?.focus(); }
    };
    window.addEventListener('pointerdown', closeOutside);
    window.addEventListener('scroll', close, true);
    window.addEventListener('keydown', escape);
    return () => {
      window.removeEventListener('pointerdown', closeOutside);
      window.removeEventListener('scroll', close, true);
      window.removeEventListener('keydown', escape);
    };
  }, [menuPosition]);
  const openMenu = (x: number, y: number) => setMenuPosition({ x: Math.max(8, Math.min(x, window.innerWidth - 168)), y: Math.max(8, Math.min(y, window.innerHeight - 52)) });
  const confirmDelete = async () => {
    setIsDeleting(true);
    setDeleteError(null);
    try {
      await useCalendarStore.getState().removeTaskDay(node.id, day);
      deleteDialog.current?.close();
    } catch { setDeleteError('Could not remove the task from this day. Please try again.'); }
    finally { setIsDeleting(false); }
  };
  const finishDrag = () => {
    previewRef.current?.remove();
    previewRef.current = null;
    cardRef.current?.classList.remove('is-dragging');
    onDragChange(false);
  };
  useEffect(() => () => { previewRef.current?.remove(); }, []);
  return (
    <article className={`week-task-card${isComplete ? ' is-done' : ''}`} data-week-task={node.id}
      ref={cardRef} draggable onDragEnd={finishDrag}
      tabIndex={0} aria-label={node.title}
      onContextMenu={event => { event.preventDefault(); event.stopPropagation(); openMenu(event.clientX, event.clientY); }}
      onKeyDown={event => {
        if (event.key === 'ContextMenu' || (event.shiftKey && event.key === 'F10')) {
          event.preventDefault();
          const box = event.currentTarget.getBoundingClientRect();
          openMenu(box.left + 12, box.top + 12);
        }
      }}
      onPointerDownCapture={event => {
        dragAllowed.current = !(event.target instanceof Element && event.target.closest('button, input, label, [contenteditable]'));
      }}
      onDragStart={event => {
        if (!dragAllowed.current) {
          event.preventDefault();
          return;
        }
        const payload = JSON.stringify({ id: node.id, parentId: node.parentId ?? null, categoryId: node.categoryId, sortOrder: node.sortOrder ?? 0, sourceDay: day });
        event.dataTransfer.setData('application/x-drasil-node', payload);
        event.dataTransfer.setData('text/plain', payload);
        event.dataTransfer.effectAllowed = 'move';
        const box = event.currentTarget.getBoundingClientRect();
        const preview = event.currentTarget.cloneNode(true) as HTMLElement;
        preview.removeAttribute('data-week-task');
        preview.removeAttribute('draggable');
        preview.setAttribute('aria-hidden', 'true');
        preview.inert = true;
        preview.classList.add('week-drag-preview');
        preview.style.width = `${box.width}px`;
        document.body.append(preview);
        previewRef.current = preview;
        event.dataTransfer.setDragImage(preview, event.clientX - box.left, event.clientY - box.top);
        event.currentTarget.classList.add('is-dragging');
        onDragChange(true);
      }}
      style={{ '--week-category': categoryAccent(category?.accentColor ?? '#6366f1') } as CSSProperties}>
      <div className="week-task-top">
        <label className="week-task-check">
          <input type="checkbox" aria-label={`Complete ${node.title}`} checked={isComplete} disabled={!planningHasDueWork(node, day, scheduledDays)}
            onChange={event => onStatusChange(node.id, event.target.checked, planningDueIds(node, day, scheduledDays))} />
        </label>
        <button type="button" className="week-task-title" onClick={() => onOpenTask(node.id)}>
          {node.emoji && <span aria-hidden="true">{node.emoji}</span>}{node.title}
        </button>
        <button type="button" className="week-task-menu-trigger" aria-label={`Options for ${node.title}`} aria-haspopup="menu" aria-expanded={!!menuPosition}
          onClick={event => { const bounds = event.currentTarget.getBoundingClientRect(); openMenu(bounds.right - 168, bounds.bottom + 4); }}>
          <MoreHorizontal size={20} aria-hidden="true" />
        </button>
      </div>
      <div className="week-task-timing">
        {blocks.length > 0 && <div className="week-task-times"><CalendarDays size={12} aria-hidden="true" />
          {blocks.map(block => <span key={block.id}>{new Date(block.startAt).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })} – {new Date(block.endAt).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })}</span>)}
        </div>}
        <span className="week-task-estimate"><Clock3 size={12} aria-hidden="true" />{planningDuration(planningEstimate(node, day, scheduledDays))}</span>
      </div>
      {subtasks.length > 0 && (
        <div className="week-task-subtasks">
          {subtasks.map(({ node: child, parentTitle, deferredDate }) => {
            const childComplete = deferredDate ? child.status === 'done' : planningCardComplete(child, day, scheduledDays);
            return <div key={child.id} className={`week-subtask${childComplete ? ' is-done' : ''}${deferredDate ? ' is-deferred' : ''}`} data-week-subtask={child.id}>
              <label className="week-subtask-check"><input type="checkbox" checked={childComplete} disabled={!deferredDate && !planningHasDueWork(child, day, scheduledDays)} aria-label={`Complete ${child.title}`}
                onChange={event => onStatusChange(child.id, event.target.checked, deferredDate ? undefined : planningDueIds(child, day, scheduledDays))} /></label>
              <button type="button" aria-label={child.title} onClick={() => onOpenTask(child.id)}>
                <span>{child.title}</span>
                {parentTitle && <small>Part of {parentTitle}</small>}
              </button>
              {deferredDate && <small className="week-subtask-day" title="Planned for another day; not required for this day's progress">
                <CalendarDays size={11} aria-hidden="true" />Planned {localDayDate(deferredDate).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}
              </small>}
            </div>;
          })}
        </div>
      )}
      <footer className="week-task-meta">
        <span className={`week-task-priority priority-${node.priority}`}><span className="week-priority-dot" aria-hidden="true" />{node.priority[0].toUpperCase() + node.priority.slice(1)} priority</span>
        <span className="week-task-category"><WorkspaceIcon slug={category?.iconSlug ?? 'circle'} size={13} /><span>{category?.name ?? 'Task'}</span></span>
      </footer>
      {menuPosition && createPortal(<div ref={menuRef} className="week-task-menu" role="menu" aria-label={`Actions for ${node.title}`}
        style={{ left: menuPosition.x, top: menuPosition.y }}>
        <button type="button" role="menuitem" onClick={() => { setMenuPosition(null); setDeleteError(null); deleteDialog.current?.showModal(); }}><Trash2 size={14} aria-hidden="true" />Remove from this day</button>
      </div>, document.body)}
      <dialog ref={deleteDialog} className="week-delete-dialog" aria-labelledby={deleteTitleId}
        onCancel={event => { if (isDeleting) event.preventDefault(); }} onClick={event => event.stopPropagation()}>
        <h2 id={deleteTitleId}>Remove “{node.title}” from this day?</h2>
        <p>This clears its plan and bookings for {localDayDate(day).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}. The task and its subtasks stay in the tree. Plans for other days stay in place.</p>
        {deleteError && <p role="alert">{deleteError}</p>}
        <div className="week-delete-actions">
          <button type="button" autoFocus disabled={isDeleting} onClick={() => { deleteDialog.current?.close(); cardRef.current?.focus(); }}>Cancel</button>
          <button type="button" className="week-delete-confirm" disabled={isDeleting} onClick={() => void confirmDelete()}>{isDeleting ? 'Removing…' : 'Remove from this day'}</button>
        </div>
      </dialog>
    </article>
  );
}

export function WeeklyPlanningBoard({ nodes, categoryById, isLoading, focusedDay, onFocusDay, onAddTask, onOpenTask, onStatusChange }: WeeklyPlanningBoardProps) {
  const today = localDayKey(new Date());
  const [range, setRange] = useState(() => ({ start: shiftDay(mondayOf(focusedDay ?? today), -7), end: shiftDay(mondayOf(focusedDay ?? today), 14) }));
  const [showCompleted, setShowCompleted] = useState(false);
  const [dropDay, setDropDay] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const weekBlocks = useCalendarStore(state => state.blocks);
  const fetchBlocks = useCalendarStore(state => state.fetchBlocks);
  const workspaceUsable = useWorkspaceStore(state => state.ready || state.legacy);
  const scrollRef = useRef<HTMLDivElement>(null);
  const reportedDay = useRef<string | null>(null);
  const scrollFrame = useRef<number | null>(null);
  const pendingDay = useRef<string | null>(focusedDay ?? today);
  const prependAnchor = useRef<{ day: string; left: number } | null>(null);
  const cardDragging = useRef(false);
  const dragPointer = useRef<{ x: number; y: number } | null>(null);
  const movingTasks = useRef(new Set<string>());
  const updateNode = useNodeStore(state => state.updateNode);
  const activeDragPayload = useNodeStore(state => state.activeDragPayload);
  const [draggingCard, setDraggingCard] = useState(false);
  const changeCardDrag = useCallback((dragging: boolean) => {
    cardDragging.current = dragging;
    setDraggingCard(dragging);
    if (!dragging) setDropDay(null);
  }, []);
  const visibleWeekStart = mondayOf(focusedDay ?? today);
  const days = useMemo(() => {
    const result: string[] = [];
    for (let day = range.start; day < range.end; day = shiftDay(day, 1)) result.push(day);
    return result;
  }, [range]);
  useEffect(() => {
    if (!workspaceUsable) return;
    void fetchBlocks(`${range.start}T00:00:00`, `${range.end}T00:00:00`, { force: false, focus: false });
  }, [range, fetchBlocks, workspaceUsable]);
  const blocksByDay = useMemo(() => {
    const result = new Map<string, Map<string, CalendarBlock[]>>();
    for (const block of weekBlocks) {
      if (block.variant === 'draft' || block.variant === 'proposed') continue;
      const firstDay = localDayKey(new Date(block.startAt));
      const lastDay = localDayKey(new Date(new Date(block.endAt).getTime() - 1));
      for (const day of days) if (day >= firstDay && day <= lastDay) {
        const tasks = result.get(day) ?? new Map<string, CalendarBlock[]>();
        tasks.set(block.taskId, [...(tasks.get(block.taskId) ?? []), block]);
        result.set(day, tasks);
      }
    }
    for (const tasks of result.values()) for (const blocks of tasks.values()) blocks.sort((a, b) => new Date(a.startAt).getTime() - new Date(b.startAt).getTime());
    return result;
  }, [weekBlocks, days]);
  const scheduledDays = useMemo(() => {
    const result = new Map<string, string[]>();
    for (const [day, tasks] of blocksByDay) for (const id of tasks.keys()) result.set(id, [...(result.get(id) ?? []), day]);
    return result;
  }, [blocksByDay]);
  const groups = useMemo(() => {
    const result = planningCardsByDay(nodes, scheduledDays);
    for (const [day, tasks] of result) {
      const startTime = (node: StrategyNode) => {
        const block = blocksByDay.get(day)?.get(node.id)?.[0];
        return block ? Math.max(localDayDate(day).getTime(), new Date(block.startAt).getTime()) : Infinity;
      };
      tasks.sort((a, b) => {
        const aTime = startTime(a), bTime = startTime(b);
        return aTime === bTime ? (a.sortOrder ?? 0) - (b.sortOrder ?? 0) : aTime < bTime ? -1 : 1;
      });
    }
    return result;
  }, [nodes, blocksByDay, scheduledDays]);

  const focusDay = useCallback((day: string) => {
    reportedDay.current = day;
    onFocusDay(day);
  }, [onFocusDay]);

  const scrollToDay = useCallback((day: string) => {
    const scroll = scrollRef.current;
    const column = scroll?.querySelector<HTMLElement>(`[data-week-day="${day}"]`);
    if (scroll && column) {
      const box = column.getBoundingClientRect();
      scroll.scrollLeft += box.left + box.width / 2 - visibleBoardBounds(scroll).center;
    }
  }, []);

  useEffect(() => {
    const scroll = scrollRef.current;
    const toolbar = scroll?.closest('.week-planning')?.querySelector('.week-toolbar');
    if (!scroll || !toolbar) return;
    let frame = 0;
    const observer = new ResizeObserver(() => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => { if (reportedDay.current) scrollToDay(reportedDay.current); });
    });
    observer.observe(scroll);
    observer.observe(toolbar);
    return () => { observer.disconnect(); cancelAnimationFrame(frame); };
  }, [scrollToDay]);

  // Adding earlier days preserves the current pixels instead of resetting a week.
  useLayoutEffect(() => {
    const scroll = scrollRef.current;
    if (scroll && prependAnchor.current) {
      const anchor = prependAnchor.current;
      const column = scroll.querySelector<HTMLElement>(`[data-week-day="${anchor.day}"]`);
      if (column) scroll.scrollLeft += column.getBoundingClientRect().left - anchor.left;
      prependAnchor.current = null;
    }
    if (pendingDay.current) {
      const day = pendingDay.current;
      pendingDay.current = null;
      scrollToDay(day);
      focusDay(day);
    }
  }, [range, focusDay, scrollToDay]);

  useEffect(() => () => {
    if (scrollFrame.current !== null) cancelAnimationFrame(scrollFrame.current);
    onFocusDay(null);
  }, [onFocusDay]);

  // Horizontal gestures move the board beneath its fixed floating panels;
  // vertical gestures continue scrolling the tree or calendar normally.
  useEffect(() => {
    const shell = scrollRef.current?.closest<HTMLElement>('.app-shell-floating');
    if (!shell) return;
    const scrollUnderPanels = (event: WheelEvent) => {
      const target = event.target instanceof Element ? event.target : null;
      if (Math.abs(event.deltaX) <= Math.abs(event.deltaY) || !target?.closest('.floating-task-repository, .panel-calendar')) return;
      const scroll = scrollRef.current;
      if (!scroll) return;
      const amount = event.deltaX * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? scroll.clientWidth : 1);
      scroll.scrollLeft += amount;
      event.preventDefault();
      event.stopPropagation();
    };
    shell.addEventListener('wheel', scrollUnderPanels, { passive: false, capture: true });
    return () => shell.removeEventListener('wheel', scrollUnderPanels, { capture: true });
  }, []);

  // Calendar navigation can focus another day in the displayed week as well.
  useEffect(() => {
    if (focusedDay && focusedDay !== reportedDay.current) {
      const frame = requestAnimationFrame(() => {
        reportedDay.current = focusedDay;
        if (focusedDay < range.start || focusedDay >= range.end) {
          pendingDay.current = focusedDay;
          setRange({ start: shiftDay(mondayOf(focusedDay), -7), end: shiftDay(mondayOf(focusedDay), 14) });
        } else scrollToDay(focusedDay);
      });
      return () => cancelAnimationFrame(frame);
    }
  }, [focusedDay, range, scrollToDay]);

  // End-of-drag cleanup runs even when the tree clears its payload first.
  useEffect(() => {
    const clearTarget = () => setDropDay(null);
    for (const type of ['pointerup', 'pointercancel', 'drop', 'dragend']) window.addEventListener(type, clearTarget, true);
    return () => { for (const type of ['pointerup', 'pointercancel', 'drop', 'dragend']) window.removeEventListener(type, clearTarget, true); };
  }, []);

  useEffect(() => {
    if (!activeDragPayload) return;
    const trackTarget = (event: PointerEvent) => {
      const column = document.elementFromPoint(event.clientX, event.clientY)?.closest<HTMLElement>('[data-week-day]');
      setDropDay(column?.dataset.weekDay ?? null);
    };
    const clearTarget = () => setDropDay(null);
    window.addEventListener('pointermove', trackTarget);
    window.addEventListener('pointerup', clearTarget);
    window.addEventListener('pointercancel', clearTarget);
    return () => {
      window.removeEventListener('pointermove', trackTarget);
      window.removeEventListener('pointerup', clearTarget);
      window.removeEventListener('pointercancel', clearTarget);
    };
  }, [activeDragPayload]);

  // Scroll at the exposed edges, including while the pointer stays still.
  useEffect(() => {
    const track = (event: MouseEvent) => { dragPointer.current = { x: event.clientX, y: event.clientY }; };
    window.addEventListener('dragover', track, true);
    window.addEventListener('pointermove', track, true);
    return () => {
      window.removeEventListener('dragover', track, true);
      window.removeEventListener('pointermove', track, true);
    };
  }, []);

  useEffect(() => {
    if (!draggingCard && !activeDragPayload) return;
    let frame = 0;
    let lastTime = 0;
    const stop = (event: Event) => {
      // Native dragging cancels pointer input as it takes over the gesture.
      if (cardDragging.current && (event.type === 'pointerup' || event.type === 'pointercancel')) return;
      dragPointer.current = null;
      cancelAnimationFrame(frame);
      changeCardDrag(false);
    };
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') stop(event); };
    const tick = (time: number) => {
      const scroll = scrollRef.current;
      const pointer = dragPointer.current;
      const elapsed = Math.min(32, lastTime ? time - lastTime : 16);
      lastTime = time;
      if (scroll && pointer) {
        const rect = scroll.getBoundingClientRect();
        const { left, right } = visibleBoardBounds(scroll);
        const edge = Math.min(64, (right - left) / 4);
        if (pointer.y >= rect.top && pointer.y <= rect.bottom && pointer.x >= left - 16 && pointer.x <= right + 16) {
          const speed = pointer.x < left + edge ? -Math.min(1, (left + edge - pointer.x) / edge)
            : pointer.x > right - edge ? Math.min(1, (pointer.x - right + edge) / edge) : 0;
          if (speed) scroll.scrollLeft += speed * elapsed * 0.85;
          const column = document.elementFromPoint(pointer.x, pointer.y)?.closest<HTMLElement>('[data-week-day]');
          setDropDay(column?.dataset.weekDay ?? null);
        }
      }
      frame = requestAnimationFrame(tick);
    };
    for (const type of ['drop', 'dragend', 'pointerup', 'pointercancel']) window.addEventListener(type, stop, true);
    window.addEventListener('keydown', escape, true);
    frame = requestAnimationFrame(tick);
    return () => {
      cancelAnimationFrame(frame);
      for (const type of ['drop', 'dragend', 'pointerup', 'pointercancel']) window.removeEventListener(type, stop, true);
      window.removeEventListener('keydown', escape, true);
    };
  }, [draggingCard, activeDragPayload, changeCardDrag]);

  const handleScroll = () => {
    if (scrollFrame.current !== null) cancelAnimationFrame(scrollFrame.current);
    scrollFrame.current = requestAnimationFrame(() => {
      const scroll = scrollRef.current;
      if (!scroll) return;
      const { left, right, center } = visibleBoardBounds(scroll);
      let nearest: { day: string; distance: number } | null = null;
      scroll.querySelectorAll<HTMLElement>('[data-week-day]').forEach(column => {
        const box = column.getBoundingClientRect();
        if (box.right <= left || box.left >= right) return;
        const distance = Math.abs(box.left + box.width / 2 - center);
        if (!nearest || distance < nearest.distance) nearest = { day: column.dataset.weekDay!, distance };
      });
      const closest = nearest as { day: string; distance: number } | null;
      if (closest && closest.day !== reportedDay.current) focusDay(closest.day);
      if (closest && closest.day <= shiftDay(range.start, 3) && !prependAnchor.current) {
        const anchor = scroll.querySelector<HTMLElement>(`[data-week-day="${range.start}"]`);
        if (anchor) prependAnchor.current = { day: range.start, left: anchor.getBoundingClientRect().left };
        setRange(current => current.start === range.start ? { start: shiftDay(current.start, -7), end: days.length >= 35 && !cardDragging.current ? shiftDay(current.end, -7) : current.end } : current);
      } else if (closest && closest.day >= shiftDay(range.end, -4)) {
        if (days.length >= 35 && !cardDragging.current) {
          const anchor = scroll.querySelector<HTMLElement>(`[data-week-day="${closest.day}"]`);
          if (anchor) prependAnchor.current = { day: closest.day, left: anchor.getBoundingClientRect().left };
        }
        setRange(current => current.end === range.end ? { start: days.length >= 35 && !cardDragging.current ? shiftDay(current.start, 7) : current.start, end: shiftDay(current.end, 7) } : current);
      }
    });
  };

  const assignDay = async (id: string, day: string, sourceDay?: string) => {
    if (sourceDay === day || movingTasks.current.has(id)) return;
    movingTasks.current.add(id);
    setError(null);
    const task = groups.get(day ?? '')?.find(node => node.id === id)
      ?? [...groups.values()].flat().find(node => node.id === id);
    try {
      if (sourceDay) {
        const moved = await moveTaskDay(id, sourceDay, day);
        useCalendarStore.getState().ingestMovedBlocks(moved);
        useNodeStore.getState().setPlannedDayFromServer(id, day);
      } else await updateNode(id, { plannedDate: day, ...(task?.isBacklog ? { isBacklog: false } : {}) });
    }
    catch { setError('Could not change the planned day. Please try again.'); }
    finally { movingTasks.current.delete(id); }
  };

  const dropTask = (event: DragEvent, day: string) => {
    event.preventDefault();
    setDropDay(null);
    try {
      const payload: unknown = JSON.parse(event.dataTransfer.getData('application/x-drasil-node') || event.dataTransfer.getData('text/plain'));
      if (payload && typeof payload === 'object' && 'id' in payload && typeof payload.id === 'string') {
        void assignDay(payload.id, day, 'sourceDay' in payload && typeof payload.sourceDay === 'string' ? payload.sourceDay : undefined);
      }
    } catch { /* Other dragged content is not a task. */ }
  };

  const navigateWeek = (offset: number) => {
    const next = shiftDay(visibleWeekStart, offset * 7);
    navigateToDay(next);
  };

  const navigateToDay = (day: string) => {
    if (day < range.start || day >= range.end) {
      pendingDay.current = day;
      setRange({ start: shiftDay(mondayOf(day), -7), end: shiftDay(mondayOf(day), 14) });
    } else {
      focusDay(day);
      scrollToDay(day);
    }
  };

  const weekLabel = `${localDayDate(visibleWeekStart).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })} – ${localDayDate(shiftDay(visibleWeekStart, 6)).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })}`;

  return (
    <section className="week-planning" aria-label="Weekly planning">
      <div className="week-toolbar">
        <div className="week-navigation">
          <button type="button" aria-label="Previous planning week" onClick={() => navigateWeek(-1)}><ChevronLeft size={17} /></button>
          <span>{weekLabel}</span>
          <button type="button" aria-label="Next planning week" onClick={() => navigateWeek(1)}><ChevronRight size={17} /></button>
          <button type="button" className="week-today" onClick={() => navigateToDay(today)}>Today</button>
        </div>
        <label className="week-completed-toggle"><input type="checkbox" checked={showCompleted} onChange={event => setShowCompleted(event.target.checked)} />Show completed</label>
      </div>
      {error && <p className="week-error" role="alert">{error}</p>}
      {isLoading && <p className="week-loading" role="status">Loading your tasks…</p>}
      <div className="week-board-scroll" ref={scrollRef} onScroll={handleScroll} tabIndex={0} aria-label="Scroll through planning days" aria-busy={isLoading}>
        <span className="week-board-edge" aria-hidden="true" />
        {days.map(day => {
          const allTasks = groups.get(day) ?? [];
          const completed = allTasks.filter(task => planningCardComplete(task, day, scheduledDays)).length;
          const progress = allTasks.length ? completed / allTasks.length * 100 : 0;
          const tasks = allTasks.filter(task => showCompleted || !planningCardComplete(task, day, scheduledDays));
          const estimate = tasks.filter(task => !planningCardComplete(task, day, scheduledDays)).reduce((sum, task) => sum + planningEstimate(task, day, scheduledDays), 0);
          return (
            <section key={day} className={`week-day-column${day === focusedDay ? ' is-focused' : ''}${day === today ? ' is-today' : ''}${day === '' ? ' is-unplanned' : ''}${dropDay === day ? ' is-drop-target' : ''}`}
              data-week-day={day || undefined} data-week-column={day || 'unplanned'}
              aria-label={day ? localDayDate(day).toLocaleDateString(undefined, { weekday: 'long', month: 'long', day: 'numeric' }) : 'Unplanned tasks'}
              onDragOver={event => { if (event.dataTransfer.types.includes('application/x-drasil-node')) { event.preventDefault(); event.dataTransfer.dropEffect = 'move'; setDropDay(day); } }}
              onDragLeave={event => { if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setDropDay(null); }}
              onDrop={event => dropTask(event, day)}>
              <header className="week-day-heading">
                <button type="button" disabled={!day} onClick={() => { focusDay(day); scrollToDay(day); }} aria-pressed={!!day && day === focusedDay}>
                  <strong>{day ? localDayDate(day).toLocaleDateString(undefined, { weekday: 'long' }) : 'Unplanned'}{day === today && <span className="week-today-mark">Today</span>}</strong>
                  <span>{day ? localDayDate(day).toLocaleDateString(undefined, { month: 'long', day: 'numeric' }) : 'Ready when you are'}</span>
                </button>
                <div className="week-day-progress" role="progressbar" aria-label={`Task progress for ${day}`}
                  aria-valuemin={0} aria-valuemax={Math.max(1, allTasks.length)} aria-valuenow={completed}
                  aria-valuetext={allTasks.length ? `${completed} of ${allTasks.length} tasks completed` : 'No tasks planned'}>
                  <span style={{ transform: `scaleX(${progress / 100})` }} />
                </div>
                <div className="week-day-summary"><span>{tasks.length} {tasks.length === 1 ? 'task' : 'tasks'}</span>{estimate > 0 && <span>{planningDuration(estimate)}</span>}</div>
              </header>
              <button type="button" className="week-add-task" onClick={() => onAddTask(day || undefined)} aria-label={day ? `Add task for ${day}` : 'Add unplanned task'}><Plus size={16} aria-hidden="true" />Add task</button>
              <div className="week-day-cards">
                {tasks.map(node => <TaskCard key={node.id} node={node} day={day} category={categoryById.get(node.categoryId)}
                  blocks={blocksByDay.get(day)?.get(node.id) ?? []}
                  scheduledDays={scheduledDays}
                  onOpenTask={onOpenTask} onStatusChange={onStatusChange} onDragChange={changeCardDrag} />)}
                {!isLoading && tasks.length === 0 && <p className="week-day-empty">Room to plan</p>}
              </div>
            </section>
          );
        })}
        <span className="week-board-edge" aria-hidden="true" />
      </div>
    </section>
  );
}
