/*
  Theme toggle script. Cycles: current → next in [light, dark, system].
  Persists to the server (signed-in users) and to localStorage (fallback).
  The server endpoint returns 204; errors are silent — the visual change
  has already happened.

  Loaded at end-of-body (not deferred) so #theme-toggle-btn is already parsed.
*/
(function () {
  var CYCLE = ['light', 'dark', 'system'];
  var btn = document.getElementById('theme-toggle-btn');
  if (!btn) return;

  function currentTheme() {
    return document.documentElement.getAttribute('data-theme') || 'system';
  }

  function applyTheme(t) {
    if (t === 'system') {
      document.documentElement.removeAttribute('data-theme');
    } else {
      document.documentElement.setAttribute('data-theme', t);
    }
  }

  function persistTheme(t) {
    try { localStorage.setItem('theme', t); } catch (e) {}
    var csrf = document.querySelector('meta[name="csrf-token"]');
    if (!csrf) return;
    fetch('/account/theme', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/x-www-form-urlencoded',
        'X-CSRF-Token': csrf.getAttribute('content')
      },
      body: 'theme=' + encodeURIComponent(t)
    }).catch(function () {});
  }

  btn.addEventListener('click', function () {
    var cur = currentTheme();
    var idx = CYCLE.indexOf(cur);
    var next = CYCLE[(idx + 1) % CYCLE.length];
    applyTheme(next);
    persistTheme(next);
  });
})();
