import { lazy, Suspense, useEffect, useRef } from 'react';
import { createPortal } from 'react-dom';
import { X } from 'lucide-react';
import { useUiStore } from '../../store/useUiStore';
import './SettingsDialog.css';

const Settings = lazy(() => import('./ChatGptAuthSettings').then(module => ({ default: module.ChatGptAuthSettings })));

export function SettingsDialog() {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const close = useUiStore(state => state.closeSettings);
  useEffect(() => {
    const trigger = document.activeElement;
    dialogRef.current?.showModal();
    return () => { if (trigger instanceof HTMLElement) trigger.focus(); };
  }, []);
  return createPortal(
    <dialog ref={dialogRef} className="settings-dialog" aria-labelledby="settings-dialog-title"
      onCancel={event => { event.preventDefault(); close(); }}>
      <header className="settings-dialog-header">
        <h2 id="settings-dialog-title">Settings</h2>
        <button type="button" className="settings-close" onClick={close} aria-label="Close settings"><X size={20} /></button>
      </header>
      <Suspense fallback={<p className="settings-loading" role="status">Loading settings…</p>}><Settings /></Suspense>
    </dialog>, document.body,
  );
}
