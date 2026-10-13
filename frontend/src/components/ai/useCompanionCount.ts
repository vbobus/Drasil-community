import { useEffect, useState } from 'react';
import { companionRequest, type CompanionRun } from '../../api/companion';

export function useCompanionCount(enabled = true) {
  const [count, setCount] = useState(0);
  useEffect(() => { let active = true;
    if (!enabled) return;
    const refresh = () => {
      if (document.hidden) return;
      void companionRequest<CompanionRun[]>('runs').then(runs => {
        if (active) setCount(runs.filter(run => !run.dismissed && run.result && !run.result.quiet).length);
      }).catch(() => { /* The inbox surfaces connection errors when opened. */ });
    };
    const initialTimer = window.setTimeout(refresh, 1000);
    const timer = window.setInterval(refresh, 120000);
    window.addEventListener('companion-changed', refresh);
    return () => { active = false; window.clearTimeout(initialTimer); window.clearInterval(timer); window.removeEventListener('companion-changed', refresh); };
  }, [enabled]);
  return count;
}
