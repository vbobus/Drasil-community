import { useEffect, useRef, useState, type ReactNode } from 'react';
import { Maximize2, MessageCircle, Minimize2, X } from 'lucide-react';
import { useMediaQuery } from '../../hooks/useMediaQuery';
import './FloatingChat.css';

export function FloatingChat({ open, onOpenChange, count, companionCount = 0, children }: {
  open: boolean; onOpenChange: (open: boolean) => void; count: number; companionCount?: number; children: ReactNode;
}) {
  const [expanded, setExpanded] = useState(false);
  const [wasOpened, setWasOpened] = useState(open);
  if (open && !wasOpened) setWasOpened(true);
  const [draggingTask, setDraggingTask] = useState(false);
  const [taskDragActive, setTaskDragActive] = useState(false);
  const compact = useMediaQuery('(max-width: 1023px)');
  const windowRef = useRef<HTMLDialogElement>(null);
  const launcherRef = useRef<HTMLButtonElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const previouslyOpen = useRef(false);
  useEffect(() => {
    const dialog = windowRef.current;
    if (!dialog) return;
    if (!open) {
      dialog.close();
      if (previouslyOpen.current) launcherRef.current?.focus({ preventScroll: true });
      previouslyOpen.current = false;
      return;
    }
    // Compact chat owns focus while open; its mounted conversation survives close().
    if (compact) dialog.showModal();
    else dialog.show();
    previouslyOpen.current = true;
    if (compact) closeRef.current?.focus({ preventScroll: true });
    else dialog.querySelector<HTMLTextAreaElement>('textarea')?.focus({ preventScroll: true });
    return () => dialog.close();
  }, [open, compact]);
  useEffect(() => {
    if (!taskDragActive) return;
    function revealCalendar(event: DragEvent) {
      const bounds = windowRef.current?.getBoundingClientRect();
      if (bounds && (event.clientX < bounds.left || event.clientX > bounds.right ||
        event.clientY < bounds.top || event.clientY > bounds.bottom)) setDraggingTask(true);
    }
    function finishDrag() { setTaskDragActive(false); setDraggingTask(false); }
    window.addEventListener('dragover', revealCalendar, true);
    window.addEventListener('dragend', finishDrag, true);
    window.addEventListener('drop', finishDrag, true);
    return () => {
      window.removeEventListener('dragover', revealCalendar, true);
      window.removeEventListener('dragend', finishDrag, true);
      window.removeEventListener('drop', finishDrag, true);
    };
  }, [taskDragActive]);
  function close() {
    onOpenChange(false);
  }
  return <>
    <dialog id="floating-chat" ref={windowRef} hidden={!open} aria-modal={compact && open ? true : undefined}
      className={`floating-chat${expanded ? ' is-expanded' : ''}${draggingTask ? ' is-dragging-task' : ''}`} aria-label="Drasil chat"
      onCancel={event => { if (event.target === event.currentTarget) { event.preventDefault(); close(); } }}
      onDragStartCapture={event => { if (event.target instanceof Element && event.target.closest('.selected-task-drag-wrapper')) setTaskDragActive(true); }}
      onKeyDown={event => {
        if (event.target instanceof Element && event.target.closest('dialog') !== event.currentTarget) return;
        if (event.key === 'Escape' && !event.defaultPrevented) { event.preventDefault(); event.stopPropagation(); close(); }
      }}>
      <header className="floating-chat-header">
        <strong>Drasil</strong>
        <div>
          {!compact && <button type="button" aria-label={expanded ? 'Restore chat size' : 'Expand chat'} onClick={() => setExpanded(value => !value)}>
            {expanded ? <Minimize2 size={18} /> : <Maximize2 size={18} />}
          </button>}
          <button ref={closeRef} type="button" aria-label="Minimize chat" autoFocus={compact} onClick={close}><X size={20} /></button>
        </div>
      </header>
      {(open || wasOpened) && children}
    </dialog>
    <button ref={launcherRef} type="button" className="chat-launcher" hidden={compact && open} aria-label={open ? 'Minimize chat' : 'Open chat'}
      aria-expanded={open} aria-controls="floating-chat" onClick={() => open ? close() : onOpenChange(true)}>
      <MessageCircle size={23} /><span>Chat</span>{companionCount > 0 ? <small aria-label={`${companionCount} companion check-ins`}>{companionCount}</small> : count > 0 && <small aria-label={`${count} selected tasks`}>{count}</small>}
    </button>
  </>;
}
