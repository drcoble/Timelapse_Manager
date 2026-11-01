/*
 * Submit guards for every form and mutating HTMX action (#70 F-R2, WP-70-D).
 *
 * A double click, or a second click while a slow request is still running,
 * used to send the same POST twice: a project created twice, a render queued
 * twice, a camera probed twice. Every listener sits on `document` (event
 * delegation), so markup swapped in by HTMX needs no re-binding.
 *
 *   - Plain POST forms (no HTMX verb; htmx prevents the default submit of its
 *     own forms): the first submit marks the form and any further submit of
 *     it is cancelled at once, so a double click cannot start a second
 *     navigation. On the next tick the submit buttons are disabled for the
 *     visual state (disabling them inside the submit event would drop the
 *     clicked button's name/value from the request). A page restored from the
 *     back/forward cache (`pageshow` with `persisted`) is unlocked again.
 *   - HTMX requests with a non-GET verb: on `htmx:beforeSend` (the request is
 *     certainly going out; a cancelled `htmx:beforeRequest` never reaches it,
 *     so nothing can stay locked) the requesting form's submit buttons, or the
 *     requesting button itself, are disabled; `htmx:afterRequest` re-enables
 *     them, whatever the outcome (success, error, timeout, abort). Inputs and
 *     selects that trigger a request on change are never disabled, so a field
 *     is not taken away mid-edit. This makes `hx-disabled-elt` the default for
 *     mutating HTMX actions.
 *   - `data-clear-on-done="<selector>"` on the requesting element: on
 *     `htmx:afterRequest`, success or failure, the referenced inputs are
 *     emptied. Used for a one-time password that must not linger after a
 *     failed request, which htmx does not swap.
 *
 * Opt-outs: `data-allow-repeat` on a form, a button or an ancestor exempts it
 * from both guards; a `[data-poll]` element itself (a background refresh) is
 * never touched. Controls *inside* a polled container (an alert's Clear
 * button, a dashboard card's Pause form) keep their guard (WP-70-E1). Only controls this script disabled are re-enabled (they carry
 * `data-tlm-submit-guard`), so a button the server rendered disabled stays so.
 */
(function () {
  "use strict";

  var GUARD_ATTR = "data-tlm-submit-guard";
  var LOCKED_ATTR = "data-tlm-submitting";
  // htmx state per request: the controls this script disabled for it.
  var guarded = typeof WeakMap === "function" ? new WeakMap() : null;

  function exempt(el) {
    if (!el) return false;
    if (el.hasAttribute && el.hasAttribute("data-poll")) return true;
    return !!(el.closest && el.closest("[data-allow-repeat]"));
  }

  function isSubmitControl(el) {
    if (!el) return false;
    var tag = el.tagName;
    if (tag === "BUTTON") return (el.getAttribute("type") || "submit").toLowerCase() === "submit";
    if (tag === "INPUT") {
      var type = (el.getAttribute("type") || "").toLowerCase();
      return type === "submit" || type === "image";
    }
    return false;
  }

  // Every submit control of a form, including those associated by form="".
  function submitControls(form) {
    var out = [];
    var els = form.elements || [];
    for (var i = 0; i < els.length; i++) {
      if (isSubmitControl(els[i])) out.push(els[i]);
    }
    return out;
  }

  // Disable the controls that are enabled now, and return the ones disabled.
  function disable(controls) {
    var done = [];
    for (var i = 0; i < controls.length; i++) {
      var el = controls[i];
      if (el.disabled || exempt(el)) continue;
      el.disabled = true;
      el.setAttribute(GUARD_ATTR, "");
      done.push(el);
    }
    return done;
  }

  function release(controls) {
    for (var i = 0; i < controls.length; i++) {
      var el = controls[i];
      if (!el.hasAttribute(GUARD_ATTR)) continue;
      el.removeAttribute(GUARD_ATTR);
      el.disabled = false;
    }
  }

  function isPlainPost(form) {
    var method = (form.getAttribute("method") || "get").toLowerCase();
    if (method !== "post") return false;
    // A form posting into another window leaves this page usable.
    var target = (form.getAttribute("target") || "").toLowerCase();
    return target === "" || target === "_self";
  }

  // ---- (a) plain POST forms -------------------------------------------------

  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!form || form.tagName !== "FORM" || e.defaultPrevented) return;
    if (!isPlainPost(form) || exempt(form)) return;
    if (form.hasAttribute(LOCKED_ATTR)) {
      // Already submitted: the first request is on its way.
      e.preventDefault();
      return;
    }
    form.setAttribute(LOCKED_ATTR, "");
    setTimeout(function () {
      // A listener that ran after this one may still have cancelled it.
      if (e.defaultPrevented) {
        form.removeAttribute(LOCKED_ATTR);
        return;
      }
      disable(submitControls(form));
    }, 0);
  });

  window.addEventListener("pageshow", function (e) {
    if (!e.persisted) return;
    var forms = document.querySelectorAll("form[" + LOCKED_ATTR + "]");
    for (var i = 0; i < forms.length; i++) forms[i].removeAttribute(LOCKED_ATTR);
    release(document.querySelectorAll("[" + GUARD_ATTR + "]"));
  });

  // ---- (b) mutating HTMX requests -------------------------------------------

  function requestControls(elt) {
    if (!elt) return [];
    if (elt.tagName === "FORM") return submitControls(elt);
    if (elt.tagName === "BUTTON" || isSubmitControl(elt)) return [elt];
    return [];
  }

  document.addEventListener("htmx:beforeSend", function (e) {
    var d = e.detail || {};
    var verb = d.requestConfig && d.requestConfig.verb;
    if (!verb || String(verb).toLowerCase() === "get") return;
    if (exempt(d.elt) || !d.xhr || !guarded) return;
    var done = disable(requestControls(d.elt));
    if (done.length) guarded.set(d.xhr, done);
  });

  // ---- (c) data-clear-on-done, then release ---------------------------------

  function clearInputs(elt) {
    var selector = elt && elt.getAttribute && elt.getAttribute("data-clear-on-done");
    if (!selector) return;
    var inputs;
    try {
      inputs = document.querySelectorAll(selector);
    } catch (err) {
      return; // not a valid selector
    }
    for (var i = 0; i < inputs.length; i++) {
      var tag = inputs[i].tagName;
      if (tag === "INPUT" || tag === "TEXTAREA") inputs[i].value = "";
    }
  }

  // htmx fires afterRequest twice when the element was swapped out (on the
  // detached element, then on its first attached ancestor); both steps are
  // idempotent.
  document.addEventListener("htmx:afterRequest", function (e) {
    var d = e.detail || {};
    clearInputs(d.elt);
    if (!guarded || !d.xhr) return;
    var done = guarded.get(d.xhr);
    if (!done) return;
    guarded.delete(d.xhr);
    release(done);
  });
})();
