// Runs before first paint: applies the saved theme (light | dark | auto) so there is no flash of the wrong theme.
try {
  const t = localStorage.getItem('itam.theme');
  document.documentElement.dataset.theme = t === 'light' || t === 'dark' ? t : 'auto';
} catch (_) { document.documentElement.dataset.theme = 'auto'; }
