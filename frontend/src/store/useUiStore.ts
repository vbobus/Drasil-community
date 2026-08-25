import { create } from 'zustand';
import {
  initializeTheme,
  persistTheme,
  type AppTheme,
} from '../lib/theme';

export type AppDestination = 'all' | 'category' | 'my-day' | 'inbox' | 'settings';

interface UiState {
  activeDestination: AppDestination;
  theme: AppTheme;
}

interface UiActions {
  setActiveDestination: (activeDestination: AppDestination) => void;
  setTheme: (theme: AppTheme) => void;
}

type UiStore = UiState & UiActions;

export const useUiStore = create<UiStore>((set) => ({
  activeDestination: 'category',
  theme: initializeTheme(),
  setActiveDestination: (activeDestination) => set({ activeDestination }),
  setTheme: (theme) => {
    persistTheme(theme);
    set({ theme });
  },
}));
