/*
 * Active-alerts panel in the page header (#70 F-R6; WP-70-E2).
 *
 * The toggle (#alerts-toggle) and the panel (#alerts-panel) are static markup;
 * the alerts poll and the clear actions only replace the badge, the count
 * label and the panel body out of band. This module owns the open state:
 *
 *   - a click on the toggle (also Enter or Space: it is a native button)
 *     opens or closes the panel, with aria-expanded kept in step;
 *   - Escape closes it and returns focus to the toggle;
 *   - a click or tap outside the toggle and the panel closes it;
 *   - while it is open the poller (#alerts-area) carries data-poll-paused, so
 *     no background refresh moves the list under the reader (poll.js honours
 *     the flag); closing it fires one refresh (tlm:refresh on the poller);
 *   - a Dismiss or Clear all replaces the panel body, which removes the
 *     focused button: focus then moves to the panel itself, not to <body>.
 *
 * Nothing depends on hover, so a touch device opens the panel with a tap.
 */
(function () {
  "use strict";

  var PAUSED_ATTR = "data-poll-paused";

  function toggle() {
    return document.getElementById("alerts-toggle");
  }

  function panel() {
    return document.getElementById("alerts-panel");
  }

  function poller() {
    return document.getElementById("alerts-area");
  }

  function isOpen() {
    var p = panel();
    return !!p && !p.hidden;
  }

  function open() {
    var t = toggle();
    var p = panel();
    if (!t || !p) return;
    p.hidden = false;
    t.setAttribute("aria-expanded", "true");
    var q = poller();
    if (q) q.setAttribute(PAUSED_ATTR, "");
  }

  function close(returnFocus) {
    var t = toggle();
    var p = panel();
    if (!t || !p || p.hidden) return;
    p.hidden = true;
    t.setAttribute("aria-expanded", "false");
    if (returnFocus) t.focus();
    var q = poller();
    if (q) {
      q.removeAttribute(PAUSED_ATTR);
      if (window.htmx) window.htmx.trigger(q, "tlm:refresh");
    }
  }

  document.addEventListener("click", function (e) {
    var t = toggle();
    var p = panel();
    if (!t || !p || !e.target || typeof e.target.closest !== "function") return;
    if (t.contains(e.target)) {
      if (isOpen()) close(false);
      else open();
      return;
    }
    // A click inside the panel stays open. The target may already be detached
    // when an earlier handler replaced it, so test the event path as well.
    if (p.contains(e.target)) return;
    if (typeof e.composedPath === "function" && e.composedPath().indexOf(p) !== -1) return;
    if (isOpen()) close(false);
  });

  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape" || !isOpen()) return;
    e.preventDefault();
    close(true);
  });

  // Focus that was inside the panel body when an out-of-band swap replaced it
  // is lost to <body>; put it on the panel so a keyboard user stays in place.
  // Noted when a control in the panel sends its request (the double-submit
  // guard disables it in flight, which may already drop its focus) and again
  // just before the swap.
  var focusInBody = false;

  function focusIsInPanel() {
    var p = panel();
    return !!p && !p.hidden && p.contains(document.activeElement);
  }

  document.addEventListener("htmx:beforeRequest", function (e) {
    var elt = e.detail && e.detail.elt;
    var p = panel();
    if (elt && p && p.contains(elt) && focusIsInPanel()) focusInBody = true;
  });

  // A failed request swaps nothing; forget the note so a later swap does not
  // move focus.
  document.addEventListener("htmx:afterRequest", function (e) {
    var d = e.detail || {};
    var p = panel();
    if (d.failed && d.elt && p && p.contains(d.elt)) focusInBody = false;
  });

  document.addEventListener("htmx:oobBeforeSwap", function (e) {
    var target = e.detail && e.detail.target;
    if (!target || target.id !== "alerts-panel-body") return;
    if (target.contains(document.activeElement)) focusInBody = true;
  });

  document.addEventListener("htmx:oobAfterSwap", function (e) {
    var target = e.detail && e.detail.target;
    if (!target || target.id !== "alerts-panel-body" || !focusInBody) return;
    focusInBody = false;
    var p = panel();
    var a = document.activeElement;
    if (p && !p.hidden && (!a || a === document.body || !document.contains(a))) p.focus();
  });
})();
