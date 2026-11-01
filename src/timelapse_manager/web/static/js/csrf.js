/*
 * One source of truth for the page's CSRF token (#70 F-R3, F-S4; #165).
 *
 * The token lives in <meta name="csrf-token">. The server masks it afresh on
 * every response, so the value differs between pages but always verifies
 * against the session; it changes meaning only when the session itself
 * changes (another tab signed out and in again).
 *
 *   - htmx:configRequest -> every htmx request sends X-CSRF-Token read from the
 *                           meta *at request time* (not a value frozen into
 *                           the markup), including background polls, so the
 *                           server can tell a page its token is stale.
 *                           htmx's `selfRequestsOnly` keeps it same-origin.
 *   - tlm:csrf           -> (HX-Trigger on /alerts/summary when the presented
 *                           token is stale) a fresh token: written into the meta
 *                           and into every `csrf_token` form field on the page.
 *   - submit (capture)   -> a plain form's `csrf_token` field is refreshed from
 *                           the meta just before it is sent, so a form rendered
 *                           earlier (a dashboard card the 204 poll never
 *                           re-renders) still submits the current token.
 *
 * A request already in flight when the token rotates can still get the CSRF
 * 403; htmx-errors.js turns that into a toast with Reload. Nothing is retried:
 * a mutation is never replayed behind the user's back.
 */
(function () {
  "use strict";

  var META_SELECTOR = 'meta[name="csrf-token"]';
  var FIELD_SELECTOR = 'input[name="csrf_token"]';
  var HEADER = "X-CSRF-Token";
  // An issued token is unpadded base64url; refuse anything else.
  var TOKEN_PATTERN = /^[A-Za-z0-9_-]{1,512}$/;

  function meta() {
    return document.querySelector(META_SELECTOR);
  }

  function currentToken() {
    var el = meta();
    return el ? el.getAttribute("content") || "" : "";
  }

  function setFields(root, token) {
    var fields = root.querySelectorAll(FIELD_SELECTOR);
    for (var i = 0; i < fields.length; i++) {
      fields[i].value = token;
    }
  }

  document.addEventListener("htmx:configRequest", function (e) {
    var token = currentToken();
    if (token && e.detail && e.detail.headers) {
      e.detail.headers[HEADER] = token;
    }
  });

  document.addEventListener("tlm:csrf", function (e) {
    var token = e.detail && e.detail.token;
    if (typeof token !== "string" || !TOKEN_PATTERN.test(token)) return;
    var el = meta();
    if (!el) return;
    el.setAttribute("content", token);
    setFields(document, token);
  });

  document.addEventListener(
    "submit",
    function (e) {
      var form = e.target;
      var token = currentToken();
      if (!token || !form || !form.elements) return;
      // form.elements also covers fields tied to the form by form="id".
      for (var i = 0; i < form.elements.length; i++) {
        var field = form.elements[i];
        if (field.name === "csrf_token" && field.type === "hidden") {
          field.value = token;
        }
      }
    },
    true
  );
})();
