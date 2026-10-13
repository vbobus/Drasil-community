try {
  document.documentElement.dataset.theme =
    localStorage.getItem('drasil.appearance-theme.v1') === 'light' ? 'light' : 'dark';
} catch {
  document.documentElement.dataset.theme = 'dark';
}
