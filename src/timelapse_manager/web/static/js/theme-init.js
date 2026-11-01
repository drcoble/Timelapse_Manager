/*
  No-flash theme initialiser (#70 F-S3).

  Loaded synchronously in <head>, before the stylesheet, so data-theme is set
  before the first paint. Signed-in pages carry the stored preference on
  <html data-theme-pref>; pages without it (sign-in, first run, error pages)
  and "system" fall back to the browser's localStorage. "system" sets no
  data-theme, so the CSS prefers-color-scheme media query decides.
*/
(function () {
  var root = document.documentElement;
  var pref = root.getAttribute('data-theme-pref');
  if (!pref || pref === 'system') {
    try { pref = localStorage.getItem('theme') || 'system'; } catch (e) {}
  }
  if (pref === 'light' || pref === 'dark') {
    root.setAttribute('data-theme', pref);
  }
})();
