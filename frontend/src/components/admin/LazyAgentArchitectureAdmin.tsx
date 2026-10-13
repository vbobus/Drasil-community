import { lazy, Suspense } from 'react';

const Admin = lazy(() => import('./AgentArchitectureAdmin').then(module => ({ default: module.AgentArchitectureAdmin })));

export function LazyAgentArchitectureAdmin() {
  return <Suspense fallback={<p role="status">Loading administration…</p>}><Admin standalone /></Suspense>;
}
