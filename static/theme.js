/* Applies the saved theme family and mode before first paint. */
(() => {
  'use strict';
  const legacy = { a: 'screen', b: 'line', c: 'grid' };
  const families = ['screen', 'line', 'grid'], modes = ['auto', 'light', 'dark'];
  const root = document.documentElement, dark = matchMedia('(prefers-color-scheme: dark)');
  let family = 'screen', mode = 'auto';
  try {
    family = localStorage.getItem('fanyu-family') || legacy[localStorage.getItem('fanyu-theme')] || 'screen';
    mode = localStorage.getItem('fanyu-mode') || 'auto';
  } catch {}
  const preview = new URLSearchParams(location.search);
  family = preview.get('family') || family;
  mode = preview.get('mode') || mode;
  if (!families.includes(family)) family = 'screen';
  if (!modes.includes(mode)) mode = 'auto';
  function apply() {
    root.dataset.family = family;
    root.dataset.pref = mode;
    root.dataset.mode = mode === 'auto' ? (dark.matches ? 'dark' : 'light') : mode;
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.content = getComputedStyle(root).getPropertyValue('--bg').trim() || '#1f1e1c';
  }
  apply();
  dark.addEventListener('change', () => mode === 'auto' && apply());
  window.fanyuTheme = {
    get: () => ({ family, mode }),
    set(next) {
      if (families.includes(next.family)) family = next.family;
      if (modes.includes(next.mode)) mode = next.mode;
      try { localStorage.setItem('fanyu-family', family); localStorage.setItem('fanyu-mode', mode); } catch {}
      apply();
      dispatchEvent(new Event('fanyu:theme'));
    },
  };
  addEventListener('DOMContentLoaded', apply);
})();
