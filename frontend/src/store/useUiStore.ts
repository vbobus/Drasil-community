import { create } from 'zustand';
import {
  initializeTheme,
  persistTheme,
  type AppTheme,
} from '../lib/theme';

export type AppDestination = 'all' | 'category' | 'my-day' | 'habits' | 'inbox' | 'settings';

interface UiState {
  activeDestination: AppDestination;
  theme: AppTheme;
  isSettingsOpen: boolean;
  settingsSection: 'connections' | 'appearance' | 'memory' | 'accounts';
}

interface UiActions {
  setActiveDestination: (activeDestination: AppDestination) => void;
  setTheme: (theme: AppTheme) => void;
  openSettings: (section?: UiState['settingsSection']) => void;
  closeSettings: () => void;
}

type UiStore = UiState & UiActions;

export const useUiStore = create<UiStore>((set) => ({
  activeDestination: 'category',
  theme: initializeTheme(),
  isSettingsOpen: false,
  settingsSection: 'connections',
  openSettings: (settingsSection = 'connections') => set({ isSettingsOpen: true, settingsSection }),
  closeSettings: () => set({ isSettingsOpen: false }),
  setActiveDestination: (activeDestination) => activeDestination === 'settings'
    ? set({ isSettingsOpen: true, settingsSection: 'connections' })
    : set({ activeDestination }),
  setTheme: (theme) => {
    persistTheme(theme);
    set({ theme });
  },
}));
