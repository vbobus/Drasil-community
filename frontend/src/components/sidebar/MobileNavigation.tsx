import { useRef } from 'react';
import { Menu } from 'lucide-react';
import { AppSidebar } from './AppSidebar';
import './MobileNavigation.css';

export function MobileNavigation() {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  function close() {
    dialogRef.current?.close();
  }
  return <>
    <header className="mobile-app-header">
      <button ref={triggerRef} type="button" aria-label="Open navigation" aria-haspopup="dialog" aria-controls="mobile-navigation"
        onClick={() => dialogRef.current?.showModal()}><Menu size={22} aria-hidden="true" /><span>Menu</span></button>
      <span className="mobile-app-wordmark">Drasil</span>
    </header>
    <dialog ref={dialogRef} id="mobile-navigation" className="mobile-navigation-dialog" aria-label="Navigation"
      onClose={() => triggerRef.current?.focus({ preventScroll: true })}
      onClick={event => { if (event.target === event.currentTarget) close(); }}>
      <AppSidebar isCollapsed={false} isMobile onToggleCollapse={close} onNavigate={close} />
    </dialog>
  </>;
}
