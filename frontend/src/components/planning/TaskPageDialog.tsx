import { useEffect, useRef, type ReactNode } from 'react';
import { createPortal } from 'react-dom';

export function TaskPageDialog({ onClose, children }: { onClose: () => void; children: ReactNode }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const trigger = document.activeElement;
    const dialog = ref.current;
    dialog?.showModal();
    dialog?.querySelector<HTMLButtonElement>('[aria-label="Close task page"]')?.focus();
    return () => { if (trigger instanceof HTMLElement) trigger.focus(); };
  }, []);
  return createPortal(<dialog ref={ref} className="task-page-dialog" aria-label="Task page"
    onKeyDownCapture={event => {
      if (event.key === 'Escape' && ref.current?.querySelector('.recurrence-dialog')) event.preventDefault();
    }}
    onCancel={event => {
      event.preventDefault();
      if (!ref.current?.querySelector('.recurrence-dialog')) onClose();
    }}>
    {children}
  </dialog>, document.body);
}
