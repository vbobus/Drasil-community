import { useEffect, useRef, useState } from 'react';
import { Moon, Sun, Plug, Palette, Brain, UserRound, ExternalLink, CheckCircle2 } from 'lucide-react';
import {
  fetchChatGptAuthStatus,
  fetchStrategistProviderSettings,
  logoutChatGptAuth,
  saveStrategistProviderSettings,
  startChatGptAuth,
  type ChatGptAuthStatus,
  type CodexModelOption,
  type StrategistProviderSettings,
} from '../../api/auth';
import {
  disconnectGoogleCalendar,
  fetchGoogleCalendarStatus,
  startGoogleCalendarConnect,
  syncGoogleCalendar,
  type GoogleCalendarStatus,
} from '../../api/calendarIntegrations';
import { RememberedPreferencesSettings } from './RememberedPreferencesSettings';
import { CompanionSettings } from './CompanionSettings';
import { AccountManagementSettings } from './AccountManagementSettings';
import { useUiStore } from '../../store/useUiStore';
import './ChatGptAuthSettings.css';

const DEFAULT_CODEX_MODEL_OPTION = '__default__';
const CUSTOM_CODEX_MODEL_OPTION = '__custom__';
const ALL_CODEX_REASONING_EFFORTS = ['low', 'medium', 'high', 'xhigh', 'max', 'ultra'];

const settingsSections = [
  { id: 'connections', label: 'Connections', icon: Plug },
  { id: 'companion', label: 'Companion', icon: Brain },
  { id: 'appearance', label: 'Appearance', icon: Palette },
  { id: 'memory', label: 'Memory', icon: Brain },
  { id: 'accounts', label: 'Account & access', icon: UserRound },
] as const;

type SettingsSection = (typeof settingsSections)[number]['id'];

const settingsSectionTitles: Record<SettingsSection, string> = {
  companion: 'Your companion',
  appearance: 'Appearance',
  connections: 'Connections',
  memory: 'Remembered preferences',
  accounts: 'Accounts & server',
};

function AppearanceSettings() {
  const theme = useUiStore((state) => state.theme);
  const setTheme = useUiStore((state) => state.setTheme);

  return (
    <section className="settings-auth-card settings-appearance-card">
      <div className="settings-provider-header">
        <div>
          <p className="settings-auth-title">Choose your theme</p>
          <p className="settings-auth-copy">
            Dark mode is the default. Your selection is saved on this device.
          </p>
        </div>
        <span className="settings-auth-pill">
          {theme === 'dark' ? 'Dark mode' : 'Light mode'}
        </span>
      </div>

      <div className="settings-theme-choice-grid" role="group" aria-label="Application theme">
        <button
          type="button"
          className={`settings-theme-choice${theme === 'dark' ? ' is-active' : ''}`}
          onClick={() => setTheme('dark')}
          aria-pressed={theme === 'dark'}
          data-testid="theme-dark-choice"
        >
          <span className="settings-theme-preview settings-theme-preview-dark" aria-hidden="true">
            <span />
            <span />
            <span />
          </span>
          <span className="settings-theme-choice-copy">
            <Moon size={17} aria-hidden="true" />
            <span>
              <strong>Dark</strong>
              <small>Deep navy surfaces with calm, clear contrast.</small>
            </span>
          </span>
        </button>

        <button
          type="button"
          className={`settings-theme-choice${theme === 'light' ? ' is-active' : ''}`}
          onClick={() => setTheme('light')}
          aria-pressed={theme === 'light'}
          data-testid="theme-light-choice"
        >
          <span className="settings-theme-preview settings-theme-preview-light" aria-hidden="true">
            <span />
            <span />
            <span />
          </span>
          <span className="settings-theme-choice-copy">
            <Sun size={17} aria-hidden="true" />
            <span>
              <strong>Light</strong>
              <small>The original bright Drasil appearance.</small>
            </span>
          </span>
        </button>
      </div>
    </section>
  );
}

function formatLastRefresh(value?: string | null) {
  if (!value) return 'Unknown';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return 'Unknown';
  }

  return new Intl.DateTimeFormat('en-US', {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  }).format(parsed);
}

function getCodexPickerValue(codexModel: string, codexModelOptions: string[]) {
  if (!codexModel) {
    return DEFAULT_CODEX_MODEL_OPTION;
  }

  return codexModelOptions.includes(codexModel) ? codexModel : CUSTOM_CODEX_MODEL_OPTION;
}

function formatReasoningEffort(option: string) {
  if (option === 'xhigh') return 'Extra high';
  return option.charAt(0).toUpperCase() + option.slice(1);
}

function getCodexModelCatalog(settings: StrategistProviderSettings | null): CodexModelOption[] {
  if (settings?.codexModelCatalog?.length) {
    return settings.codexModelCatalog;
  }
  return (settings?.codexModelOptions ?? []).map((id) => ({
    id,
    displayName: id,
    supportedReasoningEfforts: ALL_CODEX_REASONING_EFFORTS,
  }));
}

export function ChatGptAuthSettings() {
  const [activeSettingsSection, setActiveSettingsSection] = useState<SettingsSection>(useUiStore.getState().settingsSection);
  const [status, setStatus] = useState<ChatGptAuthStatus | null>(null);
  const loginFlowRef = useRef<HTMLElement>(null);
  const [providerSettings, setProviderSettings] = useState<StrategistProviderSettings | null>(null);
  const [googleCalendarStatus, setGoogleCalendarStatus] = useState<GoogleCalendarStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [googleCalendarError, setGoogleCalendarError] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isStarting, setIsStarting] = useState(false);
  const [isDisconnecting, setIsDisconnecting] = useState(false);
  const [isSavingProvider, setIsSavingProvider] = useState(false);
  const [isStartingGoogleCalendar, setIsStartingGoogleCalendar] = useState(false);
  const [isGoogleCalendarAuthPending, setIsGoogleCalendarAuthPending] = useState(false);
  const [isSyncingGoogleCalendar, setIsSyncingGoogleCalendar] = useState(false);
  const [isDisconnectingGoogleCalendar, setIsDisconnectingGoogleCalendar] = useState(false);
  const [copiedCode, setCopiedCode] = useState(false);
  const [copiedLink, setCopiedLink] = useState(false);
  const [openAiApiKey, setOpenAiApiKey] = useState('');
  const [codexModel, setCodexModel] = useState('');
  const [codexReasoningEffort, setCodexReasoningEffort] = useState('');
  const [isEditingCustomCodexModel, setIsEditingCustomCodexModel] = useState(false);
  const [openAiModel, setOpenAiModel] = useState('gpt-5-mini');
  const googleCalendarPollTimerRef = useRef<number | null>(null);

  const stopGoogleCalendarPopupPolling = () => {
    if (googleCalendarPollTimerRef.current === null) {
      return;
    }
    window.clearInterval(googleCalendarPollTimerRef.current);
    googleCalendarPollTimerRef.current = null;
  };

  const loadSettings = async () => {
    const [nextStatus, nextProviderSettings, nextGoogleCalendarStatus] = await Promise.all([
      fetchChatGptAuthStatus(),
      fetchStrategistProviderSettings(true),
      fetchGoogleCalendarStatus().catch(() => {
        setGoogleCalendarError('Calendar status could not be loaded. Try Refresh status.');
        return null;
      }),
    ]);
    setStatus(nextStatus);
    setProviderSettings(nextProviderSettings);
    setGoogleCalendarStatus(nextGoogleCalendarStatus);
    setCodexModel(nextProviderSettings.codexModel ?? '');
    setCodexReasoningEffort(nextProviderSettings.codexReasoningEffort ?? '');
    setIsEditingCustomCodexModel(
      Boolean(
        nextProviderSettings.codexModel &&
          !nextProviderSettings.codexModelOptions.includes(nextProviderSettings.codexModel),
      ),
    );
    setOpenAiModel(nextProviderSettings.openAiModel);
  };

  useEffect(() => {
    const loadInitialState = async () => {
      try {
        await loadSettings();
        setError(null);
      } catch (requestError) {
        setError(
          requestError instanceof Error
            ? requestError.message
            : 'Failed to load ChatGPT authentication status.',
        );
      } finally {
        setIsLoading(false);
      }
    };

    void loadInitialState();
  }, []);

  useEffect(() => {
    if (status?.pendingSession?.status !== 'pending') {
      return;
    }

    let checking = false;
    const interval = window.setInterval(() => {
      if (checking) return;
      checking = true;
      void fetchChatGptAuthStatus()
        .then(async (nextStatus) => [nextStatus, await fetchStrategistProviderSettings()] as const)
        .then(([nextStatus, nextProviderSettings]) => {
          setStatus(nextStatus);
          setProviderSettings(nextProviderSettings);
          setCodexModel(nextProviderSettings.codexModel ?? '');
          setCodexReasoningEffort(nextProviderSettings.codexReasoningEffort ?? '');
          setIsEditingCustomCodexModel(
            Boolean(
              nextProviderSettings.codexModel &&
                !nextProviderSettings.codexModelOptions.includes(nextProviderSettings.codexModel),
            ),
          );
          setError(null);
        })
        .catch((requestError) => {
          setError(
            requestError instanceof Error
              ? requestError.message
              : 'Failed to refresh ChatGPT authentication status.',
          );
        })
        .finally(() => { checking = false; });
    }, 2500);

    return () => {
      window.clearInterval(interval);
    };
  }, [status?.pendingSession?.status]);

  useEffect(() => {
    const handleGoogleCalendarOAuthMessage = (event: MessageEvent) => {
      const payload = event.data as { type?: string; ok?: boolean; error?: string } | null;
      if (!payload || payload.type !== 'drasil:google-calendar:oauth') {
        return;
      }

      if (googleCalendarPollTimerRef.current !== null) {
        window.clearInterval(googleCalendarPollTimerRef.current);
        googleCalendarPollTimerRef.current = null;
      }
      setIsGoogleCalendarAuthPending(false);
      setIsStartingGoogleCalendar(false);

      if (payload.ok === false) {
        setGoogleCalendarError(payload.error ?? 'Google Calendar connection was not completed.');
        return;
      }

      void fetchGoogleCalendarStatus()
        .then((nextStatus) => {
          setGoogleCalendarStatus(nextStatus);
          setGoogleCalendarError(null);
        })
        .catch((requestError) => {
          setGoogleCalendarError(
            requestError instanceof Error
              ? requestError.message
              : 'Failed to refresh Google Calendar connection status.',
          );
        });
    };

    window.addEventListener('message', handleGoogleCalendarOAuthMessage);
    return () => {
      window.removeEventListener('message', handleGoogleCalendarOAuthMessage);
      if (googleCalendarPollTimerRef.current !== null) {
        window.clearInterval(googleCalendarPollTimerRef.current);
        googleCalendarPollTimerRef.current = null;
      }
    };
  }, []);

  const refreshGoogleCalendarStatus = async () => {
    const nextStatus = await fetchGoogleCalendarStatus();
    setGoogleCalendarStatus(nextStatus);
    setGoogleCalendarError(null);
  };

  useEffect(() => {
    if (status?.pendingSession?.status === 'pending') {
      loginFlowRef.current?.scrollIntoView({ block: 'start', behavior: 'smooth' });
    }
  }, [status?.pendingSession?.sessionId, status?.pendingSession?.status]);

  const handleStart = async (restart = false) => {
    const popup = window.open('about:blank', '_blank');
    if (popup) popup.opener = null;
    setIsStarting(true);
    setCopiedCode(false);
    setCopiedLink(false);
    try {
      if (restart) await logoutChatGptAuth();
      const nextStatus = await startChatGptAuth();
      const authUrl = nextStatus.pendingSession?.verificationUri;
      if (authUrl) {
        const url = new URL(authUrl);
        if (url.protocol !== 'https:' || !['auth.openai.com', 'auth0.openai.com', 'chatgpt.com'].includes(url.hostname)) {
          throw new Error('OpenAI returned an invalid sign-in address. Please try again.');
        }
        if (popup) popup.location.href = url.href;
      } else popup?.close();
      setStatus(nextStatus);
      setError(null);
    } catch (requestError) {
      popup?.close();
      setError(
        requestError instanceof Error
          ? requestError.message
          : 'Failed to start ChatGPT authentication.',
      );
    } finally {
      setIsStarting(false);
    }
  };

  const handleSaveProvider = async (activeProvider: StrategistProviderSettings['activeProvider']) => {
    setIsSavingProvider(true);
    try {
      const nextProviderSettings = await saveStrategistProviderSettings({
        activeProvider,
        codexModel: codexModel.trim() || null,
        codexReasoningEffort: codexReasoningEffort || null,
        openAiApiKey: openAiApiKey.trim() || null,
        openAiModel: openAiModel.trim() || null,
      });
      setProviderSettings(nextProviderSettings);
      setOpenAiApiKey('');
      setCodexModel(nextProviderSettings.codexModel ?? '');
      setCodexReasoningEffort(nextProviderSettings.codexReasoningEffort ?? '');
      setIsEditingCustomCodexModel(
        Boolean(
          nextProviderSettings.codexModel &&
            !nextProviderSettings.codexModelOptions.includes(nextProviderSettings.codexModel),
        ),
      );
      setOpenAiModel(nextProviderSettings.openAiModel);
      setError(null);
    } catch (requestError) {
      setError(
        requestError instanceof Error
          ? requestError.message
          : 'Failed to update strategist provider settings.',
      );
    } finally {
      setIsSavingProvider(false);
    }
  };

  const handleClearByok = async () => {
    const activeProvider = providerSettings?.supportsChatGptLogin === false
      ? 'openai-api'
      : 'codex-cli';
    setIsSavingProvider(true);
    try {
      const nextProviderSettings = await saveStrategistProviderSettings({
        activeProvider,
        codexModel: codexModel.trim() || null,
        codexReasoningEffort: codexReasoningEffort || null,
        openAiApiKey: null,
        openAiModel: openAiModel.trim() || null,
        clearOpenAiApiKey: true,
      });
      setProviderSettings(nextProviderSettings);
      setOpenAiApiKey('');
      setCodexModel(nextProviderSettings.codexModel ?? '');
      setCodexReasoningEffort(nextProviderSettings.codexReasoningEffort ?? '');
      setIsEditingCustomCodexModel(
        Boolean(
          nextProviderSettings.codexModel &&
            !nextProviderSettings.codexModelOptions.includes(nextProviderSettings.codexModel),
        ),
      );
      setOpenAiModel(nextProviderSettings.openAiModel);
      setError(null);
    } catch (requestError) {
      setError(
        requestError instanceof Error
          ? requestError.message
          : 'Failed to clear the saved BYOK key.',
      );
    } finally {
      setIsSavingProvider(false);
    }
  };

  const handleDisconnect = async () => {
    setIsDisconnecting(true);
    try {
      const nextStatus = await logoutChatGptAuth();
      setStatus(nextStatus);
      setError(null);
    } catch (requestError) {
      setError(
        requestError instanceof Error
          ? requestError.message
          : 'Failed to disconnect ChatGPT authentication.',
      );
    } finally {
      setIsDisconnecting(false);
    }
  };

  const handleGoogleCalendarConnect = async () => {
    if (!googleCalendarStatus?.isConfigured) {
      setGoogleCalendarError(
        'Google Calendar is not configured for this Drasil build. Configure the app OAuth credentials on the backend once; users should not enter them here.',
      );
      return;
    }

    setIsStartingGoogleCalendar(true);
    setIsGoogleCalendarAuthPending(false);
    try {
      const response = await startGoogleCalendarConnect();
      const authWindow = window.open(
        response.authorizationUrl,
        'drasil-google-calendar-oauth',
        'popup=yes,width=520,height=720,menubar=no,toolbar=no,location=yes,status=no,resizable=yes,scrollbars=yes',
      );

      if (!authWindow) {
        throw new Error('Your browser blocked the Google sign-in popup. Allow popups for Drasil and try again.');
      }

      authWindow.focus();
      stopGoogleCalendarPopupPolling();
      setIsGoogleCalendarAuthPending(true);
      setGoogleCalendarError(null);
      googleCalendarPollTimerRef.current = window.setInterval(() => {
        void fetchGoogleCalendarStatus()
          .then((nextStatus) => {
            setGoogleCalendarStatus(nextStatus);
            if (nextStatus.isConnected) {
              stopGoogleCalendarPopupPolling();
              setIsGoogleCalendarAuthPending(false);
              setGoogleCalendarError(null);
              if (!authWindow.closed) {
                authWindow.close();
              }
              return;
            }
            if (authWindow.closed) {
              stopGoogleCalendarPopupPolling();
              setIsGoogleCalendarAuthPending(false);
              setGoogleCalendarError('Google sign-in closed before Drasil finished connecting.');
            }
          })
          .catch((requestError) => {
            stopGoogleCalendarPopupPolling();
            setIsGoogleCalendarAuthPending(false);
            setGoogleCalendarError(
              requestError instanceof Error
                ? requestError.message
                : 'Failed to refresh Google Calendar connection status.',
            );
          });
      }, 1200);
    } catch (requestError) {
      setGoogleCalendarError(
        requestError instanceof Error
          ? requestError.message
          : 'Failed to start Google Calendar connection.',
      );
    } finally {
      setIsStartingGoogleCalendar(false);
    }
  };

  const handleGoogleCalendarSync = async () => {
    setIsSyncingGoogleCalendar(true);
    try {
      const response = await syncGoogleCalendar('both');
      setGoogleCalendarStatus(response.status);
      setGoogleCalendarError(null);
    } catch (requestError) {
      const message =
        requestError instanceof Error
          ? requestError.message
          : 'Failed to sync Google Calendar.';
      try {
        const nextStatus = await fetchGoogleCalendarStatus();
        setGoogleCalendarStatus(nextStatus);
        if (
          nextStatus.requiresReconnect ||
          nextStatus.lastSyncError?.toLowerCase().includes('invalid_grant')
        ) {
          setGoogleCalendarError(null);
          return;
        }
      } catch {
        // Preserve the original sync error when status refresh also fails.
      }
      setGoogleCalendarError(message);
    } finally {
      setIsSyncingGoogleCalendar(false);
    }
  };

  const handleGoogleCalendarDisconnect = async () => {
    setIsDisconnectingGoogleCalendar(true);
    try {
      const nextStatus = await disconnectGoogleCalendar();
      setGoogleCalendarStatus(nextStatus);
      setGoogleCalendarError(null);
    } catch (requestError) {
      setGoogleCalendarError(
        requestError instanceof Error
          ? requestError.message
          : 'Failed to disconnect Google Calendar.',
      );
    } finally {
      setIsDisconnectingGoogleCalendar(false);
    }
  };

  const pendingSession = status?.pendingSession ?? null;
  const hasPendingManualFlow = pendingSession?.status === 'pending';
  const isConnected = Boolean(status?.isAuthenticated);
  const activeProvider = providerSettings?.activeProvider ?? 'codex-cli';
  const codexModelOptions = providerSettings?.codexModelOptions ?? [];
  const codexModelCatalog = getCodexModelCatalog(providerSettings);
  const selectedCodexModel = codexModelCatalog.find((option) => option.id === codexModel);
  const supportedReasoningEfforts =
    selectedCodexModel?.supportedReasoningEfforts.length
      ? selectedCodexModel.supportedReasoningEfforts
      : ALL_CODEX_REASONING_EFFORTS;
  const showCustomCodexModelInput =
    isEditingCustomCodexModel || Boolean(codexModel && !codexModelOptions.includes(codexModel));
  const codexPickerValue = showCustomCodexModelInput
    ? CUSTOM_CODEX_MODEL_OPTION
    : getCodexPickerValue(codexModel, codexModelOptions);
  const isGoogleCalendarConfigured = Boolean(googleCalendarStatus?.isConfigured);
  const googleCalendarRequiresReconnect = Boolean(
    googleCalendarStatus?.requiresReconnect ||
      googleCalendarStatus?.lastSyncError?.toLowerCase().includes('invalid_grant'),
  );
  const isGoogleCalendarConnected = Boolean(
    googleCalendarStatus?.isConnected && !googleCalendarRequiresReconnect,
  );
  const googleCalendarConnectDisabled =
    !isGoogleCalendarConfigured ||
    isStartingGoogleCalendar ||
    isGoogleCalendarAuthPending ||
    isSyncingGoogleCalendar;

  return (
    <article className="settings-workbench">
      <nav className="settings-navigation" aria-label="Settings sections">
        {settingsSections.map(({ id, label, icon: Icon }) => (
          <button key={id} type="button" onClick={() => setActiveSettingsSection(id)}
            aria-current={activeSettingsSection === id ? 'page' : undefined}>
            <Icon size={18} aria-hidden="true" />{label}
          </button>
        ))}
      </nav>
      <div className="settings-content" key={activeSettingsSection}>
        <header className="settings-content-heading">
          <h3>{settingsSectionTitles[activeSettingsSection]}</h3>
          <p>{activeSettingsSection === 'connections' ? 'Connect the tools you use. Your connections stay private to your Drasil account.'
            : activeSettingsSection === 'appearance' ? 'Make this workspace comfortable for you.'
            : activeSettingsSection === 'memory' ? 'Review what Drasil remembers when planning with you.'
            : activeSettingsSection === 'companion' ? 'Choose how and when you want support.'
            : 'Manage your profile and who can access this workspace.'}</p>
        </header>
        {activeSettingsSection === 'appearance' ? <AppearanceSettings />
          : activeSettingsSection === 'companion' ? <CompanionSettings />
          : activeSettingsSection === 'memory' ? <RememberedPreferencesSettings />
          : activeSettingsSection === 'accounts' ? <AccountManagementSettings /> : <>
          {error && <div className="settings-auth-error" role="alert">{error}
            <button type="button" className="planning-ghost-btn" onClick={() => { void loadSettings().then(() => setError(null)).catch(() => setError('Could not load connections. Please try again.')); }}>Try loading again</button>
          </div>}
          <section className="connection-primary" aria-labelledby="chatgpt-connection-heading" aria-busy={isLoading || isStarting}>
            <div className="connection-heading">
              <div><h4 id="chatgpt-connection-heading">ChatGPT</h4><p>Your subscription, through Codex</p></div>
              <span className={`connection-state${isConnected ? ' is-connected' : ''}`} role="status">
                {isConnected && <CheckCircle2 size={15} aria-hidden="true" />}
                {isLoading ? 'Checking…' : isConnected ? 'Connected' : hasPendingManualFlow ? 'Awaiting sign-in' : 'Not connected'}
              </span>
            </div>
            {isConnected ? <>
              <p className="connection-identity">{status?.email}<span>{status?.planType}</span></p>
              <p className="settings-auth-copy">{activeProvider === 'codex-cli' ? 'Active for your AI chats.' : 'Connected, but the OpenAI API is selected for chats.'}</p>
              <div className="settings-auth-action-row">
                {activeProvider !== 'codex-cli' && <button className="settings-auth-primary-btn" onClick={() => void handleSaveProvider('codex-cli')} disabled={isSavingProvider}>Use ChatGPT for chats</button>}
                <button className="planning-ghost-btn" onClick={() => void handleDisconnect()} disabled={isDisconnecting}>{isDisconnecting ? 'Disconnecting…' : 'Disconnect'}</button>
              </div>
            </> : <>
              {providerSettings?.chatGptHosting === 'sandbox' && <div className="connection-prerequisite">
                <strong>Before you connect</strong>
                <p>In ChatGPT, open Settings → Security and enable <b>Device code sign-in</b>. Then return here to connect.</p>
                <a href="https://chatgpt.com/#settings/Security" target="_blank" rel="noopener noreferrer">Open ChatGPT security settings <ExternalLink size={14} aria-hidden="true" /></a>
              </div>}
              {pendingSession?.status !== 'pending' && <div className="settings-auth-action-row">
                <button type="button" className="settings-auth-primary-btn" onClick={() => void handleStart()}
                  disabled={isLoading || isStarting || providerSettings?.supportsChatGptLogin === false}>
                  {isStarting ? 'Preparing secure sign-in…' : 'Continue with ChatGPT'}
                </button>
                <span className="connection-help">Opens OpenAI in a new tab</span>
              </div>}
              {isStarting && <p role="status" className="settings-auth-copy">Starting your private AI connection. This can take a few seconds.</p>}
            </>}
            {pendingSession?.status === 'pending' && (
          <section ref={loginFlowRef} className="settings-auth-card settings-auth-flow-card">
            <div className="settings-auth-flow-header">
              <div>
                <p className="settings-auth-title">Enter your code on OpenAI</p>
                <p className="settings-auth-copy">
                  {pendingSession.userCode
                    ? 'On the OpenAI page, sign in and enter the one-time code below. If prompted, enable device-code login in your ChatGPT security settings. Drasil checks the connection automatically.'
                    : 'Finish signing in on the OpenAI page, then return here. Drasil checks the connection automatically.'}
                </p>
              </div>
              <span className="settings-auth-pill pending">Waiting</span>
            </div>

            {pendingSession.userCode && <div className="settings-auth-code-block">
              <div>
                <span className="settings-auth-code-label">One-time code</span>
                <strong className="settings-auth-code">{pendingSession.userCode}</strong>
              </div>
              <button
                type="button"
                className="planning-ghost-btn"
                onClick={async () => {
                  if (!pendingSession.userCode) return;
                  await navigator.clipboard.writeText(pendingSession.userCode);
                  setCopiedCode(true);
                  window.setTimeout(() => setCopiedCode(false), 1800);
                }}
              >
                {copiedCode ? 'Copied' : 'Copy code'}
              </button>
            </div>}

            <div className="settings-auth-code-block">
              <div>
                <span className="settings-auth-code-label">Login link</span>
                <strong className="settings-auth-link">{pendingSession.verificationUri}</strong>
              </div>
              <button
                type="button"
                className="planning-ghost-btn"
                onClick={async () => {
                  if (!pendingSession.verificationUri) return;
                  await navigator.clipboard.writeText(pendingSession.verificationUri);
                  setCopiedLink(true);
                  window.setTimeout(() => setCopiedLink(false), 1800);
                }}
              >
                {copiedLink ? 'Copied' : 'Copy link'}
              </button>
            </div>

            <div className="settings-auth-action-row">
<button type="button" className="planning-ghost-btn" onClick={() => void handleStart(true)} disabled={isStarting}>Get a new code</button>
              <button
                type="button"
                className="settings-auth-primary-btn"
                onClick={() => {
                  if (pendingSession.verificationUri) {
                    window.open(pendingSession.verificationUri, '_blank', 'noopener,noreferrer');
                  }
                }}
              >
                Open ChatGPT login
              </button>
              <button
                type="button"
                className="planning-ghost-btn"
                onClick={() => void handleDisconnect()}
                disabled={isDisconnecting}
              >
                Cancel
              </button>
            </div>
          </section>
        )}


            {pendingSession && pendingSession.status !== 'pending' && pendingSession.error && <div className="settings-auth-error" role="alert">{pendingSession.error}</div>}
            <p className="connection-footnote">{providerSettings?.chatGptHosting === 'sandbox'
              ? 'AI starts when needed and sleeps after five idle minutes. Subscription limits apply. No automatic API billing.'
              : 'Uses the ChatGPT account connected to this Drasil profile.'}</p>
          </section>
          <details className="settings-disclosure"><summary>Model & reasoning <span>{codexModel || 'Codex default'}</span></summary><section className="settings-auth-card settings-codex-card">
          <div className="settings-provider-header">
            <div>
              <p className="settings-auth-title">ChatGPT login model</p>
              <p className="settings-auth-copy">
                Use the default, or choose a model your subscription can access. Reasoning summaries appear when the model supplies them.
              </p>
            </div>
            <span className="settings-auth-pill">
              {codexModel ? 'Override saved' : 'Using Codex default'}
            </span>
          </div>

          <div className="settings-auth-code-block settings-byok-field">
            <div>
              <span className="settings-auth-code-label">Preset</span>
              <strong className="settings-auth-link">{codexModel || 'Codex default'}</strong>
            </div>
            <select
              className="settings-byok-input settings-model-select"
              aria-label="ChatGPT model" value={codexPickerValue}
              onChange={(event) => {
                const value = event.target.value;
                if (value === DEFAULT_CODEX_MODEL_OPTION) {
                  setCodexModel('');
                  setCodexReasoningEffort('');
                  setIsEditingCustomCodexModel(false);
                  return;
                }
                if (value === CUSTOM_CODEX_MODEL_OPTION) {
                  setIsEditingCustomCodexModel(true);
                  if (codexModelOptions.includes(codexModel)) {
                    setCodexModel('');
                  }
                  return;
                }
                setCodexModel(value);
                const nextModel = codexModelCatalog.find((option) => option.id === value);
                if (
                  codexReasoningEffort &&
                  nextModel &&
                  !nextModel.supportedReasoningEfforts.includes(codexReasoningEffort)
                ) {
                  setCodexReasoningEffort('');
                }
                setIsEditingCustomCodexModel(false);
              }}
            >
              <option value={DEFAULT_CODEX_MODEL_OPTION}>Use Codex default</option>
              {codexModelCatalog.map((option) => (
                <option key={option.id} value={option.id}>
                  {option.displayName}
                </option>
              ))}
              <option value={CUSTOM_CODEX_MODEL_OPTION}>Custom model…</option>
            </select>
          </div>

          {showCustomCodexModelInput && (
            <div className="settings-auth-code-block settings-byok-field">
              <div>
                <span className="settings-auth-code-label">Custom model</span>
                <strong className="settings-auth-link">{codexModel || 'Enter a model id'}</strong>
              </div>
              <input
                type="text"
                className="settings-byok-input"
                aria-label="Custom model" value={codexModel}
                onChange={(event) => setCodexModel(event.target.value)}
                placeholder="gpt-5.1-codex-mini"
                autoComplete="off"
              />
            </div>
          )}

          <div className="settings-auth-code-block settings-byok-field">
            <div>
              <span className="settings-auth-code-label">Thinking level</span>
              <strong className="settings-auth-link">
                {codexReasoningEffort
                  ? formatReasoningEffort(codexReasoningEffort)
                  : `Model default${selectedCodexModel?.defaultReasoningEffort ? ` (${formatReasoningEffort(selectedCodexModel.defaultReasoningEffort)})` : ''}`}
              </strong>
            </div>
            <select
              className="settings-byok-input settings-model-select"
              aria-label="Thinking level" value={codexReasoningEffort}
              onChange={(event) => setCodexReasoningEffort(event.target.value)}
            >
              <option value="">
                Model default
                {selectedCodexModel?.defaultReasoningEffort
                  ? ` (${formatReasoningEffort(selectedCodexModel.defaultReasoningEffort)})`
                  : ''}
              </option>
              {supportedReasoningEfforts.map((effort) => (
                <option key={effort} value={effort}>
                  {formatReasoningEffort(effort)}
                </option>
              ))}
            </select>
          </div>

          {selectedCodexModel?.description && (
            <p className="settings-model-hint">{selectedCodexModel.description}</p>
          )}

          <p className="settings-model-hint">
            These settings apply to ChatGPT. Saving does not switch your active connection.
          </p>

          <div className="settings-auth-action-row">
            <button
              type="button"
              className="settings-auth-primary-btn"
              onClick={() => void handleSaveProvider(activeProvider)}
              disabled={isSavingProvider}
            >
              {isSavingProvider ? 'Saving...' : 'Save model settings'}
            </button>
          </div>
        </section></details>
          <section className="connection-secondary">
            <h4>Other connections</h4>
            <details className="settings-disclosure"><summary>Google Calendar <span>{isGoogleCalendarConnected ? 'Connected' : isGoogleCalendarConfigured ? 'Not connected' : 'Setup required'}</span></summary><section className="settings-auth-card settings-calendar-card">
          <div className="settings-provider-header">
            <div>
              <p className="settings-auth-title">Google Calendar</p>
              <p className="settings-auth-copy">
                Mirror Drasil blocks into a dedicated Drasil calendar. Drasil stays the source
                of truth; Google time edits sync back, and Google title or description edits are
                ignored.
              </p>
            </div>
            <span className="settings-auth-pill">
              {isGoogleCalendarConnected
                ? 'Connected'
                : googleCalendarRequiresReconnect
                  ? 'Reconnect'
                : isGoogleCalendarConfigured
                  ? 'Ready'
                  : 'Setup required'}
            </span>
          </div>

          <div className="settings-auth-status-grid">
            <div className="settings-auth-status-item">
              <span>Calendar</span>
              <strong>{googleCalendarStatus?.calendarSummary ?? 'Drasil'}</strong>
            </div>
            <div className="settings-auth-status-item">
              <span>Mode</span>
              <strong>Dedicated</strong>
            </div>
            <div className="settings-auth-status-item">
              <span>Done state</span>
              <strong>[Done] prefix</strong>
            </div>
            <div className="settings-auth-status-item">
              <span>Last sync</span>
              <strong>{formatLastRefresh(googleCalendarStatus?.lastSyncedAt)}</strong>
            </div>
          </div>

          {googleCalendarStatus?.lastSyncError && (
            <div className="settings-auth-error" role="alert">
              {googleCalendarRequiresReconnect
                ? 'Your Google Calendar connection has expired or was revoked. Reconnect Google Calendar to continue syncing.'
                : googleCalendarStatus.lastSyncError}
            </div>
          )}

          {googleCalendarError && (
            <div className="settings-auth-error" role="alert">
              {googleCalendarError}
            </div>
          )}

          <div className="settings-auth-action-row">
            <button
              type="button"
              className="settings-auth-primary-btn"
              onClick={() =>
                isGoogleCalendarConnected
                  ? void handleGoogleCalendarSync()
                  : void handleGoogleCalendarConnect()
              }
              disabled={isGoogleCalendarConnected ? isSyncingGoogleCalendar : googleCalendarConnectDisabled}
            >
              {isGoogleCalendarConnected
                ? isSyncingGoogleCalendar
                  ? 'Syncing...'
                  : 'Sync Google Calendar'
                : !isGoogleCalendarConfigured
                  ? 'Google Calendar unavailable'
                : isGoogleCalendarAuthPending
                  ? 'Waiting for Google...'
                : isStartingGoogleCalendar
                  ? 'Opening...'
                  : googleCalendarRequiresReconnect
                    ? 'Reconnect Google Calendar'
                  : 'Connect Google Calendar'}
            </button>
            <button
              type="button"
              className="planning-ghost-btn"
              onClick={() => void refreshGoogleCalendarStatus()}
            >
              Refresh status
            </button>
            <button
              type="button"
              className="planning-ghost-btn"
              onClick={() => void handleGoogleCalendarDisconnect()}
              disabled={!googleCalendarStatus?.isConnected || isDisconnectingGoogleCalendar}
            >
              {isDisconnectingGoogleCalendar ? 'Disconnecting...' : 'Disconnect Google'}
            </button>
          </div>

          {!isGoogleCalendarConfigured && (
            <p className="settings-model-hint">
              Google Calendar sign-in is not available in this build yet. App-level Google
              OAuth credentials must be configured once before users can connect with Google.
            </p>
          )}
        </section></details>
            <details className="settings-disclosure"><summary>OpenAI API <span>{activeProvider === 'openai-api' ? 'Active · separately billed' : 'Optional · separately billed'}</span></summary>
              <p className="settings-auth-copy">API usage is billed separately from ChatGPT. Connect only if you want to use API credits.</p><section className="settings-auth-card settings-byok-card">
          <div className="settings-provider-header">
            <div>
              <p className="settings-auth-title">OpenAI API</p>
              <p className="settings-auth-copy">
                {providerSettings?.supportsChatGptLogin === false
                  ? 'Your key is encrypted and saved privately for your Drasil account.'
                  : 'Save an OpenAI API key privately for this account to use with Strategist.'}
              </p>
            </div>
            <span className="settings-auth-pill">
              {providerSettings?.isOpenAiKeyConfigured ? 'Key saved' : 'No key'}
            </span>
          </div>

          <div className="settings-auth-code-block settings-byok-field">
            <div>
              <span className="settings-auth-code-label">OpenAI API key</span>
              <strong className="settings-auth-link">
                {providerSettings?.maskedOpenAiKey ?? 'No key saved'}
              </strong>
            </div>
            <input
              type="password"
              className="settings-byok-input"
              aria-label="OpenAI API key" value={openAiApiKey}
              onChange={(event) => setOpenAiApiKey(event.target.value)}
              placeholder="Enter a new key or leave blank to keep the existing one"
              autoComplete="off"
            />
          </div>

          <div className="settings-auth-code-block settings-byok-field">
            <div>
              <span className="settings-auth-code-label">Model</span>
              <strong className="settings-auth-link">{openAiModel || 'gpt-5-mini'}</strong>
            </div>
            <input
              type="text"
              className="settings-byok-input"
              aria-label="OpenAI API model" value={openAiModel}
              onChange={(event) => setOpenAiModel(event.target.value)}
              placeholder="gpt-5-mini"
              autoComplete="off"
            />
          </div>

          <div className="settings-auth-action-row">
            <button
              type="button"
              className="settings-auth-primary-btn"
              onClick={() => void handleSaveProvider('openai-api')}
              disabled={isSavingProvider}
            >
              {isSavingProvider ? 'Saving...' : 'Save and use API key'}
            </button>
            <button
              type="button"
              className="planning-ghost-btn"
              onClick={() => void handleClearByok()}
              disabled={isSavingProvider || !providerSettings?.isOpenAiKeyConfigured}
            >
              Clear saved key
            </button>
          </div>
        </section>
            </details>
          </section>
        </>}
      </div>
    </article>
  );
}
