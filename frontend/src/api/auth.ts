import { apiRequest } from './http';

const BASE = '/api/v1/auth/chatgpt';
const PROVIDER_BASE = '/api/v1/auth/strategist-provider';

export interface ChatGptAuthSession {
  sessionId: string;
  status: 'pending' | 'completed' | 'failed' | 'cancelled';
  verificationUri?: string | null;
  userCode?: string | null;
  error?: string | null;
}

export interface ChatGptAuthStatus {
  provider: 'codex-cli';
  isAuthenticated: boolean;
  authMode?: string | null;
  planType?: string | null;
  email?: string | null;
  name?: string | null;
  accountId?: string | null;
  lastRefresh?: string | null;
  pendingSession?: ChatGptAuthSession | null;
}

export interface StrategistProviderSettings {
  supportsChatGptLogin?: boolean;
  chatGptHosting?: 'sandbox' | 'local';
  activeProvider: 'codex-cli' | 'openai-api';
  activeProviderLabel: string;
  codexModel?: string | null;
  codexModelOptions: string[];
  codexModelCatalog?: CodexModelOption[];
  codexReasoningEffort?: string | null;
  isOpenAiKeyConfigured: boolean;
  maskedOpenAiKey?: string | null;
  openAiModel: string;
}

export interface CodexModelOption {
  id: string;
  displayName: string;
  description?: string | null;
  defaultReasoningEffort?: string | null;
  supportedReasoningEfforts: string[];
}

export async function fetchChatGptAuthStatus(): Promise<ChatGptAuthStatus> {
  return apiRequest<ChatGptAuthStatus>(BASE);
}

export async function startChatGptAuth(): Promise<ChatGptAuthStatus> {
  return apiRequest<ChatGptAuthStatus>(`${BASE}/start`, { method: 'POST' });
}

export async function logoutChatGptAuth(): Promise<ChatGptAuthStatus> {
  return apiRequest<ChatGptAuthStatus>(`${BASE}/logout`, { method: 'POST' });
}

export async function fetchStrategistProviderSettings(
  refreshCatalog = false,
): Promise<StrategistProviderSettings> {
  const query = refreshCatalog ? '?refresh_catalog=true' : '';
  return apiRequest<StrategistProviderSettings>(`${PROVIDER_BASE}${query}`);
}

export async function saveStrategistProviderSettings(data: {
  activeProvider: StrategistProviderSettings['activeProvider'];
  codexModel?: string | null;
  codexReasoningEffort?: string | null;
  openAiApiKey?: string | null;
  openAiModel?: string | null;
  clearOpenAiApiKey?: boolean;
}): Promise<StrategistProviderSettings> {
  return apiRequest<StrategistProviderSettings>(PROVIDER_BASE, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      activeProvider: data.activeProvider,
      codexModel: data.codexModel ?? null,
      codexReasoningEffort: data.codexReasoningEffort ?? null,
      openAiApiKey: data.openAiApiKey ?? null,
      openAiModel: data.openAiModel ?? null,
      clearOpenAiApiKey: data.clearOpenAiApiKey ?? false,
    }),
  });
}
