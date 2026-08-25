import { useEffect, useRef, useState } from 'react';
import { Moon, Sun } from 'lucide-react';
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
import { AccountManagementSettings } from './AccountManagementSettings';
import { useUiStore } from '../../store/useUiStore';
import './ChatGptAuthSettings.css';

const DEFAULT_CODEX_MODEL_OPTION = '__default__';
const CUSTOM_CODEX_MODEL_OPTION = '__custom__';
const ALL_CODEX_REASONING_EFFORTS = ['low', 'medium', 'high', 'xhigh', 'max', 'ultra'];

const settingsSections = [
  { id: 'appearance', label: 'Appearance' },
  { id: 'connections', label: 'Connections' },
  { id: 'memory', label: 'Remembered preferences' },
  { id: 'accounts', label: 'Accounts & server' },
] as const;

type SettingsSection = (typeof settingsSections)[number]['id'];

const settingsSectionTitles: Record<SettingsSection, string> = {
  appearance: 'Appearance',
  connections: 'Strategist providers',
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
  const [activeSettingsSection, setActiveSettingsSection] = useState<SettingsSection>('appearance');
  const [status, setStatus] = useState<ChatGptAuthStatus | null>(null);
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
      fetchGoogleCalendarStatus(),
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

    const interval = window.setInterval(() => {
      void Promise.all([fetchChatGptAuthStatus(), fetchStrategistProviderSettings()])
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
        });
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

  const handleStart = async () => {
    setIsStarting(true);
    setCopiedCode(false);
    setCopiedLink(false);
    try {
      const nextStatus = await startChatGptAuth();
      setStatus(nextStatus);
      setError(null);
    } catch (requestError) {
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
    const activeProvider = providerSettings?.activeProvider === 'openai-api' ? 'codex-cli' : (providerSettings?.activeProvider ?? 'codex-cli');
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
  const summaryTitle = hasPendingManualFlow
    ? 'Finish connecting ChatGPT'
    : isConnected
      ? 'Connected to ChatGPT'
      : 'Not connected';
  const summaryCopy = hasPendingManualFlow
    ? 'Use the link and one-time code below in any browser. Once you finish sign-in manually, this panel will detect completion automatically.'
    : isConnected
      ? 'This machine is already connected. If you want to switch to a different ChatGPT account here, disconnect first and then start the manual device-code flow.'
      : 'Strategist uses your local Codex login for the MVP. Start the device-code flow here, finish it in the browser, and the app will detect completion automatically.';
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
    <article className="planning-surface planning-settings-surface">
      <div className="planning-surface-header">
        <div>
          <p className="planning-surface-label">Settings</p>
          <h3 className="planning-surface-title">{settingsSectionTitles[activeSettingsSection]}</h3>
        </div>
        {activeSettingsSection === 'connections' && (
          <span className="settings-auth-provider">
            Active: {providerSettings?.activeProviderLabel ?? 'ChatGPT login'}
          </span>
        )}
      </div>

      <nav className="settings-submenu" aria-label="Settings sections">
        {settingsSections.map((section) => (
          <button
            key={section.id}
            type="button"
            className={`settings-submenu-btn${activeSettingsSection === section.id ? ' is-active' : ''}`}
            onClick={() => setActiveSettingsSection(section.id)}
            aria-current={activeSettingsSection === section.id ? 'page' : undefined}
          >
            {section.label}
          </button>
        ))}
      </nav>

      <div className="planning-ai-scroll settings-auth-scroll">
        {activeSettingsSection === 'appearance' ? (
          <AppearanceSettings />
        ) : activeSettingsSection === 'memory' ? (
          <RememberedPreferencesSettings />
        ) : activeSettingsSection === 'accounts' ? (
          <AccountManagementSettings />
        ) : (
          <>
        <section className="settings-auth-card settings-provider-card">
          <div className="settings-provider-header">
            <div>
              <p className="settings-auth-title">Choose the Strategist provider</p>
              <p className="settings-auth-copy">
                Keep ChatGPT login for the Codex flow, or switch Strategist to BYOK with
                your own OpenAI API key.
              </p>
            </div>
            <span className="settings-auth-pill">{providerSettings?.activeProviderLabel ?? 'ChatGPT login'}</span>
          </div>

          <div className="settings-provider-choice-grid">
            <button
              type="button"
              className={`settings-provider-choice${activeProvider === 'codex-cli' ? ' is-active' : ''}`}
              onClick={() => void handleSaveProvider('codex-cli')}
              disabled={isSavingProvider}
            >
              <strong>ChatGPT login</strong>
              <span>Uses the local Codex session on this machine.</span>
            </button>

            <button
              type="button"
              className={`settings-provider-choice${activeProvider === 'openai-api' ? ' is-active' : ''}`}
              onClick={() => void handleSaveProvider('openai-api')}
              disabled={isSavingProvider || !providerSettings?.isOpenAiKeyConfigured}
              title={
                providerSettings?.isOpenAiKeyConfigured
                  ? 'Use BYOK (OpenAI API)'
                  : 'Save an API key below before switching to BYOK'
              }
            >
              <strong>BYOK (OpenAI API)</strong>
              <span>
                {providerSettings?.isOpenAiKeyConfigured
                  ? `Saved key: ${providerSettings.maskedOpenAiKey}`
                  : 'No API key saved yet'}
              </span>
            </button>
          </div>
        </section>

        <section className="settings-auth-card settings-codex-card">
          <div className="settings-provider-header">
            <div>
              <p className="settings-auth-title">ChatGPT login model</p>
              <p className="settings-auth-copy">
                Choose any model available to your ChatGPT login, including Sol, Terra, and
                Luna, and set how much thinking it should use.
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
              value={codexPickerValue}
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
                value={codexModel}
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
              value={codexReasoningEffort}
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
            This only changes the ChatGPT-login path. It does not switch the active provider
            unless you explicitly choose ChatGPT login above.
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
        </section>

        <section className="settings-auth-card settings-byok-card">
          <div className="settings-provider-header">
            <div>
              <p className="settings-auth-title">Bring your own key</p>
              <p className="settings-auth-copy">
                Save a local OpenAI API key for this machine and use it as the Strategist
                provider when you want to move off the ChatGPT login path.
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
                {providerSettings?.maskedOpenAiKey ?? 'No key saved locally'}
              </strong>
            </div>
            <input
              type="password"
              className="settings-byok-input"
              value={openAiApiKey}
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
              value={openAiModel}
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
              {isSavingProvider ? 'Saving...' : 'Save and use BYOK'}
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

        <section className="settings-auth-card settings-calendar-card">
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
                  : 'Unavailable'}
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
        </section>

        <section className="settings-auth-card settings-auth-summary-card">
          <div>
            <p className="settings-auth-title">{summaryTitle}</p>
            <p className="settings-auth-copy">{summaryCopy}</p>
          </div>

          <div className="settings-auth-status-grid">
            <div className="settings-auth-status-item">
              <span>Status</span>
              <strong>{status?.isAuthenticated ? 'Connected' : 'Disconnected'}</strong>
            </div>
            <div className="settings-auth-status-item">
              <span>Plan</span>
              <strong>{status?.planType ?? 'Unknown'}</strong>
            </div>
            <div className="settings-auth-status-item">
              <span>Email</span>
              <strong>{status?.email ?? 'Unknown'}</strong>
            </div>
            <div className="settings-auth-status-item">
              <span>Last refresh</span>
              <strong>{formatLastRefresh(status?.lastRefresh)}</strong>
            </div>
          </div>
        </section>

        {pendingSession?.status === 'pending' && (
          <section className="settings-auth-card settings-auth-flow-card">
            <div className="settings-auth-flow-header">
              <div>
                <p className="settings-auth-title">Finish sign-in in your browser</p>
                <p className="settings-auth-copy">
                  Open the ChatGPT device login page, enter the one-time code, and return here.
                  This panel polls status automatically.
                </p>
              </div>
              <span className="settings-auth-pill pending">Waiting</span>
            </div>

            <div className="settings-auth-code-block">
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
            </div>

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

        {pendingSession && pendingSession.status !== 'pending' && pendingSession.error && (
          <div className="settings-auth-error" role="alert">
            {pendingSession.error}
          </div>
        )}

        {error && (
          <div className="settings-auth-error" role="alert">
            {error}
          </div>
        )}

        <div className="settings-auth-action-row">
          <button
            type="button"
            className="settings-auth-primary-btn"
            onClick={() => void handleStart()}
            disabled={isLoading || isStarting || pendingSession?.status === 'pending' || isConnected}
          >
            {isStarting ? 'Starting...' : isConnected ? 'Connected' : 'Connect ChatGPT'}
          </button>
          <button
            type="button"
            className="planning-ghost-btn"
            onClick={() => void handleDisconnect()}
            disabled={!status?.isAuthenticated || isDisconnecting}
          >
            {isDisconnecting ? 'Disconnecting...' : 'Disconnect'}
          </button>
        </div>
          </>
        )}
      </div>
    </article>
  );
}
