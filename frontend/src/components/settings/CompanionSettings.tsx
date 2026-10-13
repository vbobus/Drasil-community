import { useEffect, useState } from 'react';
import { companionRequest, type CompanionSettings as Settings } from '../../api/companion';
import '../ai/Companion.css';

export function CompanionSettings() {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [hours, setHours] = useState('9, 18');
  const [token, setToken] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  useEffect(() => { let active = true;
    companionRequest<Settings>('settings').then(value => { if (active) { setSettings(value); setHours(value.check_in_hours.join(', ')); } })
      .catch(e => { if (active) setError(e.message); });
    return () => { active = false; };
  }, []);
  async function action(fn: () => Promise<void>) {
    setBusy(true); setError(''); setNotice('');
    try { await fn(); } catch (e) { setError(e instanceof Error ? e.message : 'Please retry.'); }
    finally { setBusy(false); }
  }
  if (!settings) return <p role={error ? 'alert' : 'status'}>{error || 'Loading companion settings…'}</p>;
  return <form className="companion-settings" onSubmit={e => { e.preventDefault(); void action(async () => {
    const parsed = hours.trim() ? hours.split(',').map(value => Number(value.trim())) : [];
    if (parsed.length > 3 || parsed.some(value => !Number.isInteger(value) || value < 0 || value > 23)) throw new Error('Choose up to three hours from 0 to 23, separated by commas.');
    setSettings(await companionRequest<Settings>('settings', 'PUT', { ...settings, check_in_hours: parsed }));
    setNotice('Companion settings saved.');
  }); }}>
    <div><h4>A little follow-through</h4><p>Hermes checks in on your goals. Hindsight remembers context and past conversations.</p></div>
    <div className="companion-connection"><strong>{settings.paired ? 'Worker paired' : 'Connect your worker'}</strong>
      <span>Hermes: {settings.worker_status.replaceAll('_', ' ')} · Memory: {settings.memory_status.replaceAll('_', ' ')}</span>
      {settings.last_seen_at && <small>Last seen {new Date(settings.last_seen_at + 'Z').toLocaleString()}</small>}
    </div>
    <label className="companion-toggle"><input type="checkbox" checked={settings.enabled} disabled={!settings.paired || busy}
      onChange={e => setSettings({ ...settings, enabled: e.target.checked })} />Enable companion</label>
    <label>What matters to you<textarea rows={5} maxLength={12000} value={settings.personal_context}
      placeholder="Your goals, responsibilities, and how you like to be supported…"
      onChange={e => setSettings({ ...settings, personal_context: e.target.value })} /></label>
    <div className="companion-fields"><label>Time zone<input value={settings.time_zone} required
      onChange={e => setSettings({ ...settings, time_zone: e.target.value })} /></label>
      <label>Check-in hours<input value={hours} placeholder="9, 18" onChange={e => setHours(e.target.value)} />
        <small>24-hour clock. Leave empty for replies only.</small></label>
      <label>Daily response limit<input type="number" min={1} max={12} required value={settings.daily_run_limit}
        onChange={e => setSettings({ ...settings, daily_run_limit: Number(e.target.value) })} /></label></div>
    <p>Check-ins appear in the companion inbox. Proposed task changes still need your review. Memory processing also uses your subscription allowance.</p>
    {error && <p role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
    <div className="companion-actions"><button type="submit" disabled={busy}>{busy ? 'Saving…' : 'Save settings'}</button>
      <button type="button" disabled={busy} onClick={() => void action(async () => {
        const value = await companionRequest<{ token: string }>('pair', 'POST'); setToken(value.token);
        setSettings(await companionRequest<Settings>('settings'));
      })}>{settings.paired ? 'Replace worker key' : 'Create worker key'}</button>
      {settings.paired && <button type="button" disabled={busy} onClick={() => void action(async () => {
        setSettings(await companionRequest<Settings>('pair', 'DELETE')); setToken(''); setNotice('Worker disconnected. Saved memories are retained.');
      })}>Disconnect</button>}</div>
    {token && <label>Worker key — shown only here<input readOnly value={token} onFocus={e => e.target.select()} autoComplete="off" />
      <small>Save this as DRASIL_WORKER_TOKEN on your private worker. Replacing it revokes the old key.</small>
      <button type="button" onClick={() => setToken('')}>Hide key</button></label>}
  </form>;
}
