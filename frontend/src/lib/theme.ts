export type AppTheme = 'dark' | 'light';

export const THEME_STORAGE_KEY = 'drasil.appearance-theme.v1';
export const DEFAULT_THEME: AppTheme = 'dark';

export function readStoredTheme(): AppTheme {
  try {
    return window.localStorage.getItem(THEME_STORAGE_KEY) === 'light'
      ? 'light'
      : DEFAULT_THEME;
  } catch {
    return DEFAULT_THEME;
  }
}

export function applyTheme(theme: AppTheme) {
  document.documentElement.dataset.theme = theme;
  document.documentElement.style.colorScheme = theme;
  document
    .querySelector('meta[name="theme-color"]')
    ?.setAttribute('content', theme === 'dark' ? '#0b1120' : '#f8fafc');
}

export function persistTheme(theme: AppTheme) {
  applyTheme(theme);
  try {
    window.localStorage.setItem(THEME_STORAGE_KEY, theme);
  } catch {
    // The theme still applies for this session when storage is unavailable.
  }
}

export function initializeTheme(): AppTheme {
  const theme = readStoredTheme();
  applyTheme(theme);
  return theme;
}
