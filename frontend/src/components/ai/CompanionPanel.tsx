import { useEffect, useState } from 'react';
import { companionRequest, type CompanionRun } from '../../api/companion';
import { ChatMarkdown } from './ChatMarkdown';
import './Companion.css';

export function CompanionPanel({ onPlan }: { onPlan: (prompt: string) => void }) {
  const [runs, setRuns] = useState<CompanionRun[]>([]);
  const [prompt, setPrompt] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [loaded, setLoaded] = useState(false);
  useEffect(() => { let active = true;
    const refresh = () => companionRequest<CompanionRun[]>('runs').then(value => { if (active) { setRuns(value); setLoaded(true); setError(''); } })
      .catch(e => { if (active) setError(e.message); });
    void refresh(); const timer = window.setInterval(() => { if (!document.hidden) void refresh(); }, 15000);
    return () => { active = false; window.clearInterval(timer); };
  }, []);
  const visible = runs.filter(run => !run.dismissed && !run.result?.quiet).reverse();
  return <section className="companion-panel" aria-label="Companion inbox">
    <div className="companion-feed" aria-live="polite">
      {loaded && visible.length === 0 && <div className="companion-empty"><h3>Make room for what matters.</h3>
        <p>Talk through a goal, a responsibility, or something you keep putting off.</p>
        <p>Connect your worker in Settings → Companion to get started.</p></div>}
      {!loaded && !error && <p role="status">Loading check-ins…</p>}
      {visible.map(run => <article className="companion-entry" key={run.id}>
        <div className="companion-entry-meta"><time dateTime={run.created_at}>{new Date(run.created_at + 'Z').toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })}</time>
          <button type="button" aria-label="Dismiss check-in" onClick={() => {
            void companionRequest<CompanionRun>(`runs/${run.id}/dismiss`, 'POST').then(value => {
              setRuns(current => current.map(item => item.id === value.id ? value : item));
              window.dispatchEvent(new Event('companion-changed'));
            }).catch(e => setError(e.message));
          }}>Dismiss</button></div>
        {run.kind === 'message' && <p className="companion-user-message">{run.prompt}</p>}
        {run.result ? <><ChatMarkdown text={run.result.message} />{run.result.suggested_prompt &&
          <button type="button" onClick={() => onPlan(run.result!.suggested_prompt!)}>Draft a plan</button>}</>
          : <p role={run.error ? 'alert' : 'status'}>{run.error || (run.status === 'running' ? 'Thinking it through…' : 'Waiting for your worker. Responses resume within your daily limit.')}</p>}
      </article>)}
    </div>
    {error && <p role="alert">{error}</p>}
    <form className="companion-composer" onSubmit={async e => { e.preventDefault(); if (!prompt.trim()) return;
      setBusy(true); setError(''); try { const value = await companionRequest<CompanionRun>('runs', 'POST', { prompt });
        setRuns(current => [value, ...current]); setPrompt('');
      } catch (e) { setError(e instanceof Error ? e.message : 'Please retry.'); } finally { setBusy(false); }
    }}><label className="sr-only" htmlFor="companion-message">Message your companion</label>
      <textarea id="companion-message" rows={3} maxLength={4000} value={prompt} onChange={e => setPrompt(e.target.value)} placeholder="What would help you move forward?" />
      <button type="submit" disabled={busy || !prompt.trim()}>{busy ? 'Sending…' : 'Send'}</button>
    </form>
  </section>;
}
