/*
  Browser timezone detection. Fires once per page; if the detected IANA name
  differs from the stored value the difference is POSTed silently. This keeps
  the viewer timezone current without requiring manual input.

  The server-rendered stored value is passed in via window.__viewerTz, set by a
  tiny inline shim in the page <head>/end-of-body before this script loads.
*/
(function () {
  var storedTz = window.__viewerTz;
  var detected;
  try {
    detected = Intl.DateTimeFormat().resolvedOptions().timeZone;
  } catch (e) { return; }
  if (!detected || detected === storedTz) return;
  var csrf = document.querySelector('meta[name="csrf-token"]');
  if (!csrf || !csrf.getAttribute('content')) return;
  fetch('/account/timezone', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/x-www-form-urlencoded',
      'X-CSRF-Token': csrf.getAttribute('content')
    },
    body: 'timezone=' + encodeURIComponent(detected)
  }).catch(function () {});
})();
