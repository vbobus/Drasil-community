import { useEffect } from 'react';

// CSS consumers include dialogs portaled outside App. Geometry updates don't
// rerender the workspace and are coalesced into one write per animation frame.
export function useVisualViewport() {
  useEffect(() => {
    const viewport = window.visualViewport;
    const style = document.documentElement.style;
    let frame = 0;
    const update = () => {
      frame = 0;
      if (viewport && Math.abs(viewport.scale - 1) > 0.01) return;
      const height = viewport?.height ?? window.innerHeight;
      const top = viewport?.offsetTop ?? 0;
      const values = {
        '--visual-viewport-height': height,
        '--visual-viewport-width': viewport?.width ?? window.innerWidth,
        '--visual-viewport-top': top,
        '--visual-viewport-left': viewport?.offsetLeft ?? 0,
        '--keyboard-inset': Math.max(0, window.innerHeight - height - top),
      };
      for (const [name, value] of Object.entries(values)) style.setProperty(name, `${value}px`);
    };
    const schedule = () => { if (!frame) frame = requestAnimationFrame(update); };
    update();
    window.addEventListener('resize', schedule);
    viewport?.addEventListener('resize', schedule);
    viewport?.addEventListener('scroll', schedule);
    return () => {
      cancelAnimationFrame(frame);
      window.removeEventListener('resize', schedule);
      viewport?.removeEventListener('resize', schedule);
      viewport?.removeEventListener('scroll', schedule);
      for (const name of ['height', 'width', 'top', 'left']) style.removeProperty(`--visual-viewport-${name}`);
      style.removeProperty('--keyboard-inset');
    };
  }, []);
}
