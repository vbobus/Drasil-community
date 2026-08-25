import { type FormEvent, useEffect, useState } from 'react';
import { changePassword, createAccount, fetchAccounts } from '../../api/accounts';
import {
  clearSession,
  getBackendUrl,
  normalizeBackendUrl,
  setBackendUrl,
  setSession,
  type DrasilAccount,
} from '../../api/connection';
import { useDrasilAccount } from '../account/AccountContext';

export function AccountManagementSettings() {
  const { account, backendLabel, openAccountSwitcher, signOut } = useDrasilAccount();
  const [accounts, setAccounts] = useState<DrasilAccount[]>([]);
  const [displayName, setDisplayName] = useState('');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [isAdmin, setIsAdmin] = useState(false);
  const [serverUrl, setServerUrl] = useState(getBackendUrl());
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [isSaving, setIsSaving] = useState(false);
  const [currentPassword, setCurrentPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');

  const refreshAccounts = () => {
    void fetchAccounts().then(setAccounts).catch((loadError) => {
      setError(loadError instanceof Error ? loadError.message : 'Could not load accounts.');
    });
  };

  useEffect(refreshAccounts, []);

  const addAccount = async (event: FormEvent) => {
    event.preventDefault();
    setError(null);
    setNotice(null);
    setIsSaving(true);
    try {
      const created = await createAccount({ displayName, username, password, isAdmin });
      setAccounts((current) => [...current, created]);
      setDisplayName('');
      setUsername('');
      setPassword('');
      setIsAdmin(false);
      setNotice(`${created.displayName} can now sign in and configure a private workspace.`);
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : 'Could not create the account.');
    } finally {
      setIsSaving(false);
    }
  };

  const useServer = () => {
    setError(null);
    try {
      const normalized = normalizeBackendUrl(serverUrl);
      setBackendUrl(normalized);
      clearSession();
      window.location.reload();
    } catch (serverError) {
      setError(serverError instanceof Error ? serverError.message : 'That server address is invalid.');
    }
  };

  const updatePassword = async (event: FormEvent) => {
    event.preventDefault();
    setError(null);
    setNotice(null);
    setIsSaving(true);
    try {
      const session = await changePassword(currentPassword, newPassword);
      setSession(session.token, session.account);
      setCurrentPassword('');
      setNewPassword('');
      setNotice('Password updated. Other signed-in tabs for this account were signed out.');
    } catch (passwordError) {
      setError(passwordError instanceof Error ? passwordError.message : 'Could not update the password.');
    } finally {
      setIsSaving(false);
    }
  };

  return (
    <>
      <section className="settings-auth-card">
        <div className="settings-provider-header">
          <div>
            <p className="settings-auth-title">Signed-in account</p>
            <p className="settings-auth-copy">
              Tasks, calendars, provider logins, API keys, and remembered preferences belong only
              to this account.
            </p>
          </div>
          <span className="settings-auth-pill">@{account.username}</span>
        </div>
        <div className="settings-account-summary">
          <span className="settings-account-avatar">{account.displayName.slice(0, 1).toLocaleUpperCase()}</span>
          <div><strong>{account.displayName}</strong><small>{account.isAdmin ? 'Administrator' : 'Member'}</small></div>
        </div>
        <div className="settings-auth-action-row">
          <button type="button" className="settings-auth-primary-btn" onClick={openAccountSwitcher}>
            Switch account
          </button>
          <button type="button" className="planning-ghost-btn" onClick={() => void signOut()}>
            Sign out
          </button>
        </div>
      </section>
      {error && <div className="account-error" role="alert">{error}</div>}
      {notice && <div className="settings-account-notice" role="status">{notice}</div>}

      <form className="settings-auth-card" onSubmit={(event) => void updatePassword(event)}>
        <div>
          <p className="settings-auth-title">Change password</p>
          <p className="settings-auth-copy">Use at least 10 characters. Updating it signs out other tabs for this account.</p>
        </div>
        <label className="settings-account-field"><span>Current password</span><input type="password" value={currentPassword} onChange={(event) => setCurrentPassword(event.target.value)} required autoComplete="current-password" /></label>
        <label className="settings-account-field"><span>New password</span><input type="password" value={newPassword} onChange={(event) => setNewPassword(event.target.value)} required minLength={10} autoComplete="new-password" /></label>
        <div className="settings-auth-action-row"><button type="submit" className="planning-ghost-btn" disabled={isSaving}>{isSaving ? 'Updating…' : 'Update password'}</button></div>
      </form>

      <section className="settings-auth-card">
        <div className="settings-provider-header">
          <div>
            <p className="settings-auth-title">Backend server</p>
            <p className="settings-auth-copy">
              Use the same host for a one-machine install, or point this frontend at a secured VPS.
            </p>
          </div>
          <span className="settings-auth-pill">Connected</span>
        </div>
        <div className="settings-server-current"><span>Current</span><strong>{backendLabel}</strong></div>
        <label className="settings-account-field">
          <span>New backend address</span>
          <input
            value={serverUrl}
            onChange={(event) => setServerUrl(event.target.value)}
            placeholder="Blank uses this website; otherwise https://drasil-api.example.com"
          />
        </label>
        <p className="settings-model-hint">
          Changing server signs this tab out. Use HTTPS for a remote backend, or keep the backend
          private behind Tailscale or an SSH tunnel.
        </p>
        <div className="settings-auth-action-row">
          <button type="button" className="planning-ghost-btn" onClick={useServer}>Change server</button>
        </div>
      </section>

      <section className="settings-auth-card">
        <div className="settings-provider-header">
          <div>
            <p className="settings-auth-title">People on this Drasil server</p>
            <p className="settings-auth-copy">Each person signs in separately and receives an empty, isolated workspace.</p>
          </div>
          <span className="settings-auth-pill">{accounts.length} account{accounts.length === 1 ? '' : 's'}</span>
        </div>
        <div className="settings-account-list">
          {accounts.map((candidate) => (
            <div key={candidate.id}>
              <span className="settings-account-avatar">{candidate.displayName.slice(0, 1).toLocaleUpperCase()}</span>
              <span><strong>{candidate.displayName}</strong><small>@{candidate.username}</small></span>
              <small>{candidate.isAdmin ? 'Admin' : 'Member'}</small>
            </div>
          ))}
        </div>
      </section>

      {account.isAdmin && (
        <form className="settings-auth-card" onSubmit={(event) => void addAccount(event)}>
          <div>
            <p className="settings-auth-title">Add an account</p>
            <p className="settings-auth-copy">The new account starts with default categories and no personal integrations.</p>
          </div>
          <div className="settings-account-form-grid">
            <label className="settings-account-field"><span>Display name</span><input value={displayName} onChange={(event) => setDisplayName(event.target.value)} required maxLength={80} /></label>
            <label className="settings-account-field"><span>Username</span><input value={username} onChange={(event) => setUsername(event.target.value)} required minLength={3} maxLength={48} autoComplete="off" /></label>
          </div>
          <label className="settings-account-field"><span>Temporary password</span><input type="password" value={password} onChange={(event) => setPassword(event.target.value)} required minLength={10} autoComplete="new-password" /></label>
          <label className="settings-account-admin-check"><input type="checkbox" checked={isAdmin} onChange={(event) => setIsAdmin(event.target.checked)} /><span>Allow this person to add other accounts</span></label>
          <div className="settings-auth-action-row"><button type="submit" className="settings-auth-primary-btn" disabled={isSaving}>{isSaving ? 'Creating…' : 'Create account'}</button></div>
        </form>
      )}
    </>
  );
}
