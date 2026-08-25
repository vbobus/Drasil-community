import {
  type FormEvent,
  type ReactNode,
  useEffect,
  useMemo,
  useState,
} from 'react';
import {
  bootstrapAccount,
  fetchAccounts,
  fetchAccountStatus,
  fetchCurrentAccount,
  loginAccount,
  logoutAccount,
} from '../../api/accounts';
import {
  backendDisplayName,
  clearSession,
  DRASIL_UNAUTHORIZED_EVENT,
  getBackendUrl,
  getRememberedAccount,
  getSessionToken,
  normalizeBackendUrl,
  setBackendUrl,
  setSession,
  type DrasilAccount,
} from '../../api/connection';
import drasilLogoUrl from '../../assets/drasil-logo-transparent.png';
import { AccountContext, type AccountContextValue } from './AccountContext';
import './AccountBoundary.css';

type AccountPhase = 'checking' | 'bootstrap' | 'login' | 'ready';

interface SignInPanelProps {
  mode: 'bootstrap' | 'login' | 'switch';
  accounts?: DrasilAccount[];
  currentAccount?: DrasilAccount | null;
  onCancel?: () => void;
  onSubmit: (values: {
    username: string;
    password: string;
    displayName: string;
    bootstrapToken: string;
  }) => Promise<void>;
}

function SignInPanel({
  mode,
  accounts = [],
  currentAccount,
  onCancel,
  onSubmit,
}: SignInPanelProps) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [bootstrapToken, setBootstrapToken] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setError(null);
    setIsSubmitting(true);
    try {
      await onSubmit({ username, password, displayName, bootstrapToken });
    } catch (submitError) {
      setError(submitError instanceof Error ? submitError.message : 'Sign-in failed.');
      setIsSubmitting(false);
    }
  };

  const isBootstrap = mode === 'bootstrap';
  return (
    <form className="account-card" onSubmit={(event) => void submit(event)}>
      <div className="account-card-heading">
        <img src={drasilLogoUrl} alt="" />
        <div>
          <p className="account-eyebrow">Drasil</p>
          <h1>
            {isBootstrap
              ? 'Create the first account'
              : mode === 'switch'
                ? 'Switch account'
                : 'Sign in'}
          </h1>
        </div>
      </div>
      <p className="account-intro">
        {isBootstrap
          ? 'This first account becomes the administrator. Every account receives its own private tasks, calendar connections, and AI configuration.'
          : 'Choose a Drasil account and enter its password. Data stays separate even though everyone shares this server.'}
      </p>

      {mode === 'switch' && accounts.length > 0 && (
        <div className="account-choice-grid" role="list" aria-label="Available accounts">
          {accounts.map((candidate) => (
            <button
              key={candidate.id}
              type="button"
              className={username === candidate.username ? 'is-selected' : ''}
              onClick={() => setUsername(candidate.username)}
              disabled={candidate.id === currentAccount?.id}
            >
              <span>{candidate.displayName.slice(0, 1).toLocaleUpperCase()}</span>
              <strong>{candidate.displayName}</strong>
              <small>@{candidate.username}</small>
            </button>
          ))}
        </div>
      )}

      {isBootstrap && (
        <label>
          <span>Display name</span>
          <input
            value={displayName}
            onChange={(event) => setDisplayName(event.target.value)}
            autoComplete="name"
            required
            maxLength={80}
            placeholder="Alex"
          />
        </label>
      )}
      <label>
        <span>Username</span>
        <input
          value={username}
          onChange={(event) => setUsername(event.target.value)}
          autoComplete="username"
          required
          minLength={3}
          maxLength={48}
          placeholder="alex"
        />
      </label>
      <label>
        <span>Password</span>
        <input
          type="password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          autoComplete={isBootstrap ? 'new-password' : 'current-password'}
          required
          minLength={10}
          placeholder="At least 10 characters"
        />
      </label>
      {isBootstrap && (
        <label>
          <span>Remote setup token</span>
          <input
            type="password"
            value={bootstrapToken}
            onChange={(event) => setBootstrapToken(event.target.value)}
            autoComplete="off"
            placeholder="DRASIL_BOOTSTRAP_TOKEN"
          />
          <small>Required when the backend does not see this browser as local, including many container and VPS setups.</small>
        </label>
      )}
      {error && <div className="account-error" role="alert">{error}</div>}
      <div className="account-actions">
        <button type="submit" className="account-primary" disabled={isSubmitting}>
          {isSubmitting ? 'Connecting…' : isBootstrap ? 'Create account' : 'Sign in'}
        </button>
        {onCancel && (
          <button type="button" onClick={onCancel} disabled={isSubmitting}>Cancel</button>
        )}
      </div>
    </form>
  );
}

function ServerPanel({ onConnected }: { onConnected: () => Promise<void> }) {
  const [serverUrl, setServerUrl] = useState(getBackendUrl());
  const [error, setError] = useState<string | null>(null);
  const [isChecking, setIsChecking] = useState(false);

  const connect = async (event: FormEvent) => {
    event.preventDefault();
    setError(null);
    setIsChecking(true);
    try {
      const normalized = normalizeBackendUrl(serverUrl);
      setBackendUrl(normalized);
      clearSession();
      await onConnected();
    } catch (connectError) {
      setError(connectError instanceof Error ? connectError.message : 'Could not reach that server.');
      setIsChecking(false);
    }
  };

  return (
    <form className="account-server-card" onSubmit={(event) => void connect(event)}>
      <label>
        <span>Backend server</span>
        <input
          value={serverUrl}
          onChange={(event) => setServerUrl(event.target.value)}
          inputMode="url"
          placeholder="Leave blank when frontend and backend share a host"
        />
      </label>
      <p>
        Blank uses this website’s server. For a VPS, enter its HTTPS address, such as
        {' '}<code>https://drasil-api.example.com</code>.
      </p>
      {error && <div className="account-error" role="alert">{error}</div>}
      <button type="submit" disabled={isChecking}>
        {isChecking ? 'Checking…' : 'Use this server'}
      </button>
    </form>
  );
}

export function AccountBoundary({ children }: { children: ReactNode }) {
  const [phase, setPhase] = useState<AccountPhase>('checking');
  const [account, setAccount] = useState<DrasilAccount | null>(getRememberedAccount());
  const [connectionError, setConnectionError] = useState<string | null>(null);
  const [isSwitching, setIsSwitching] = useState(false);
  const [accounts, setAccounts] = useState<DrasilAccount[]>([]);

  const checkConnection = async () => {
    setPhase('checking');
    setConnectionError(null);
    try {
      const status = await fetchAccountStatus();
      if (!status.authenticationRequired) {
        setAccount({
          id: 'isolated-test-account',
          username: 'test-user',
          displayName: 'Test User',
          isAdmin: true,
        });
        setPhase('ready');
        return;
      }
      if (status.needsBootstrap) {
        clearSession();
        setAccount(null);
        setPhase('bootstrap');
        return;
      }
      if (getSessionToken()) {
        try {
          const current = await fetchCurrentAccount();
          setAccount(current);
          setPhase('ready');
          return;
        } catch {
          clearSession();
        }
      }
      setAccount(null);
      setPhase('login');
    } catch (error) {
      setConnectionError(error instanceof Error ? error.message : 'Could not reach the backend.');
      setPhase('login');
    }
  };

  useEffect(() => {
    const timer = window.setTimeout(() => void checkConnection(), 0);
    return () => window.clearTimeout(timer);
  }, []);

  useEffect(() => {
    const handleUnauthorized = () => {
      clearSession();
      setAccount(null);
      setIsSwitching(false);
      setPhase('login');
    };
    window.addEventListener(DRASIL_UNAUTHORIZED_EVENT, handleUnauthorized);
    return () => window.removeEventListener(DRASIL_UNAUTHORIZED_EVENT, handleUnauthorized);
  }, []);

  const finishSession = (token: string, nextAccount: DrasilAccount) => {
    setSession(token, nextAccount);
    window.location.reload();
  };

  const submitCredentials: SignInPanelProps['onSubmit'] = async (values) => {
    const session = phase === 'bootstrap'
      ? await bootstrapAccount({
          username: values.username,
          password: values.password,
          displayName: values.displayName,
          bootstrapToken: values.bootstrapToken || undefined,
        })
      : await loginAccount({ username: values.username, password: values.password });
    finishSession(session.token, session.account);
  };

  const openAccountSwitcher = () => {
    setIsSwitching(true);
    void fetchAccounts().then(setAccounts).catch(() => setAccounts([]));
  };

  const signOut = async () => {
    try {
      await logoutAccount();
    } finally {
      clearSession();
      window.location.reload();
    }
  };

  const contextValue = useMemo<AccountContextValue | null>(() => account ? ({
    account,
    backendLabel: backendDisplayName(),
    openAccountSwitcher,
    signOut,
  }) : null, [account]);

  if (phase === 'checking') {
    return <div className="account-page"><div className="account-loading">Connecting to Drasil…</div></div>;
  }

  if (phase !== 'ready' || account === null || contextValue === null) {
    return (
      <main className="account-page">
        <div className="account-stack">
          {connectionError && (
            <div className="account-connection-error" role="alert">
              <strong>Backend unavailable</strong>
              <span>{connectionError}</span>
            </div>
          )}
          {!connectionError && (
            <SignInPanel mode={phase === 'bootstrap' ? 'bootstrap' : 'login'} onSubmit={submitCredentials} />
          )}
          <ServerPanel onConnected={checkConnection} />
        </div>
      </main>
    );
  }

  return (
    <AccountContext.Provider value={contextValue}>
      {children}
      {isSwitching && (
        <div className="account-modal-backdrop" role="presentation">
          <div role="dialog" aria-modal="true" aria-label="Switch Drasil account">
            <SignInPanel
              mode="switch"
              accounts={accounts}
              currentAccount={account}
              onCancel={() => setIsSwitching(false)}
              onSubmit={submitCredentials}
            />
          </div>
        </div>
      )}
    </AccountContext.Provider>
  );
}
