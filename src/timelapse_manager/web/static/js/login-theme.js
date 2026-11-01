/*
  Theme toggle for the pre-authentication pages (sign-in). Cycles
  light -> dark -> system and remembers the choice in localStorage only: there
  is no session to persist it to. Loaded at end-of-body so the button exists.
*/
(function () {
  var CYCLE = ['light', 'dark', 'system'];
  var btn = document.getElementById('login-theme-toggle');
  if (!btn) return;
  function current() {
    return document.documentElement.getAttribute('data-theme') || 'system';
  }
  function apply(t) {
    if (t === 'system') {
      document.documentElement.removeAttribute('data-theme');
    } else {
      document.documentElement.setAttribute('data-theme', t);
    }
    try { localStorage.setItem('theme', t); } catch (e) {}
  }
  btn.addEventListener('click', function () {
    var i = CYCLE.indexOf(current());
    apply(CYCLE[(i + 1) % CYCLE.length]);
  });
})();
