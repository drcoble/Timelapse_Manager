/*
 * Visible failures for HTMX requests (#70 F-R1): a toast API, a connection
 * banner, and per-element timeout overrides.
 *
 * htmx 1.9 does not swap a 4xx/5xx response and fires only events for a
 * network error or a timeout, so before this script a failed click simply did
 * nothing. Every listener here sits on `document` (event delegation), so markup
 * swapped in by HTMX needs no re-binding.
 *
 *   - htmx:responseError  -> an error toast whose text comes from the response:
 *                            the `.inline-error` fragment the server's error
 *                            handlers return, else a JSON string `detail`, else
 *                            a message for the status code. A 5xx quotes the
 *                            response's X-Request-ID so a report can be matched
 *                            to the server log.
 *   - tlm:csrf-failed     -> (HX-Trigger on the CSRF 403) a toast that explains
 *                            the session changed and offers Reload.
 *   - htmx:sendError      -> the shared "Connection lost" banner.
 *   - htmx:timeout        -> a toast for a user action; the banner for a poll.
 *   - htmx:afterRequest   -> any response at all (status > 0) clears the banner.
 *   - htmx:configRequest  -> `data-htmx-timeout="none"` on the element or an
 *                            ancestor lifts the global 30 s timeout. htmx reads
 *                            `hx-request` timeouts as `value || config.timeout`,
 *                            so `{"timeout":0}` cannot express "no limit"; an
 *                            upload of up to 512 MB needs exactly that.
 *
 * A `[data-poll]` element itself (a background refresh) never raises a
 * per-request toast; a broken connection shows only the shared banner. A user
 * action inside a polled container (an alert's Clear button) still does.
 *
 * `window.tlmToast(message, {kind, actionLabel, onAction, requestId})` is the
 * public toast API for other scripts. Toasts are built with createElement and
 * textContent only -- server text is never parsed into the live page -- and are
 * dismissed through one delegated listener (no inline `onclick`), which also
 * serves the server-rendered flash toasts in `#toast-region`.
 */
(function () {
  "use strict";

  var MAX_TOASTS = 4;
  var AUTO_DISMISS_MS = 8000;
  var MAX_MESSAGE_LENGTH = 300;
  var BANNER_ID = "connection-banner";
  var CSRF_EVENT = "tlm:csrf-failed";

  var STATUS_MESSAGES = {
    401: "Your session has ended. Sign in again.",
    403: "You do not have permission to do that.",
    404: "That item no longer exists.",
    409: "That conflicts with a change made elsewhere. Reload and try again.",
    413: "Upload too large.",
    422: "The request was not valid.",
    429: "Too many requests. Wait a moment and try again."
  };

  function region() {
    var el = document.getElementById("toast-region");
    if (el) return el;
    el = document.createElement("div");
    el.id = "toast-region";
    el.className = "toast-region";
    el.setAttribute("role", "status");
    el.setAttribute("aria-live", "polite");
    document.body.appendChild(el);
    return el;
  }

  function clip(text) {
    var s = String(text == null ? "" : text).trim();
    return s.length > MAX_MESSAGE_LENGTH ? s.slice(0, MAX_MESSAGE_LENGTH - 1) + "…" : s;
  }

  function findDuplicate(host, kind, text) {
    var toasts = host.querySelectorAll(".toast");
    for (var i = 0; i < toasts.length; i++) {
      var msg = toasts[i].querySelector(".toast-message");
      if (toasts[i].classList.contains(kind) && msg && msg.textContent === text) {
        return toasts[i];
      }
    }
    return null;
  }

  /**
   * Show a toast. Returns the toast element.
   *
   * @param {string} message  plain text (never parsed as HTML)
   * @param {object} [opts]
   *   kind:        "error" (default) | "warning" | "info" | "success"
   *   actionLabel: label of an optional action button
   *   onAction:    called when the action button is pressed
   *   requestId:   the X-Request-ID the toast is about (kept on data-request-id)
   */
  function tlmToast(message, opts) {
    opts = opts || {};
    var kind = opts.kind || "error";
    var text = clip(message);
    var host = region();

    // The same failure repeated (a retried click, a polled endpoint) refreshes
    // one toast instead of stacking copies.
    var existing = findDuplicate(host, kind, text);
    if (existing) return existing;

    var toast = document.createElement("div");
    toast.className = "toast " + kind;
    // Errors interrupt; everything else waits for a pause in speech.
    toast.setAttribute("role", kind === "error" ? "alert" : "status");
    if (opts.requestId) toast.setAttribute("data-request-id", String(opts.requestId));

    var body = document.createElement("div");
    body.className = "toast-body";
    var msg = document.createElement("div");
    msg.className = "toast-message";
    msg.textContent = text;
    body.appendChild(msg);

    if (opts.actionLabel && typeof opts.onAction === "function") {
      var action = document.createElement("button");
      action.type = "button";
      action.className = "toast-action";
      action.textContent = opts.actionLabel;
      action.addEventListener("click", function () {
        toast.remove();
        opts.onAction();
      });
      body.appendChild(action);
    }
    toast.appendChild(body);

    var close = document.createElement("button");
    close.type = "button";
    close.className = "toast-close";
    close.setAttribute("aria-label", "Dismiss notification");
    close.textContent = "×";
    toast.appendChild(close);

    host.appendChild(toast);
    var all = host.querySelectorAll(".toast");
    for (var i = 0; i < all.length - MAX_TOASTS; i++) all[i].remove();

    // An error stays until dismissed: it may describe lost work.
    if (kind !== "error") {
      setTimeout(function () { toast.remove(); }, AUTO_DISMISS_MS);
    }
    return toast;
  }

  // ---- connection banner -------------------------------------------------

  function banner() {
    var el = document.getElementById(BANNER_ID);
    if (el) return el;
    el = document.createElement("div");
    el.id = BANNER_ID;
    el.className = "connection-banner";
    el.setAttribute("role", "alert");
    el.hidden = true;
    el.textContent = "Connection lost – retrying.";
    document.body.appendChild(el);
    return el;
  }

  function showBanner() { banner().hidden = false; }

  function hideBanner() {
    var el = document.getElementById(BANNER_ID);
    if (el) el.hidden = true;
  }

  // ---- response -> message -----------------------------------------------

  function header(xhr, name) {
    try { return xhr.getResponseHeader(name); } catch (e) { return null; }
  }

  function inlineErrorText(xhr) {
    var type = header(xhr, "Content-Type") || "";
    if (type.indexOf("text/html") === -1 || !xhr.responseText) return null;
    // DOMParser builds an inert document: no script runs, nothing loads.
    var doc = new DOMParser().parseFromString(xhr.responseText, "text/html");
    var el = doc.querySelector(".inline-error");
    var text = el ? el.textContent.trim() : "";
    return text || null;
  }

  function jsonDetail(xhr) {
    var type = header(xhr, "Content-Type") || "";
    if (type.indexOf("json") === -1) return null;
    try {
      var body = JSON.parse(xhr.responseText);
      return body && typeof body.detail === "string" && body.detail ? body.detail : null;
    } catch (e) {
      return null;
    }
  }

  function messageFor(xhr) {
    var status = xhr.status;
    var inline = inlineErrorText(xhr);
    if (inline) return inline;
    if (status >= 500) {
      var rid = header(xhr, "X-Request-ID");
      return rid ? "Server error (request id " + rid + ")" : "Server error";
    }
    // A bare 404/413 detail ("Not Found") says less than the status message.
    if (status !== 404 && status !== 413) {
      var detail = jsonDetail(xhr);
      if (detail) return detail;
    }
    return STATUS_MESSAGES[status] || "Request failed (HTTP " + status + ").";
  }

  function isPoll(elt) {
    return !!(elt && elt.hasAttribute && elt.hasAttribute("data-poll"));
  }

  function isCsrfFailure(xhr) {
    return xhr.status === 403 && (header(xhr, "HX-Trigger") || "").indexOf(CSRF_EVENT) !== -1;
  }

  function reloadPage() { window.location.reload(); }

  // ---- listeners -----------------------------------------------------------

  document.addEventListener("click", function (e) {
    var btn = e.target && e.target.closest ? e.target.closest(".toast-close") : null;
    if (!btn) return;
    var toast = btn.closest(".toast");
    if (toast) toast.remove();
  });

  document.addEventListener("htmx:configRequest", function (e) {
    var elt = e.target;
    if (elt && elt.closest && elt.closest('[data-htmx-timeout="none"]')) {
      e.detail.timeout = 0;
    }
  });

  document.addEventListener(CSRF_EVENT, function () {
    tlmToast("Your session changed in another tab. Reload the page to continue.", {
      kind: "error",
      actionLabel: "Reload",
      onAction: reloadPage
    });
  });

  document.addEventListener("htmx:responseError", function (e) {
    var xhr = e.detail && e.detail.xhr;
    if (!xhr || isPoll(e.detail.elt)) return;
    // The HX-Trigger already raised the CSRF toast (tlm:csrf-failed).
    if (isCsrfFailure(xhr)) return;
    var opts = { kind: "error" };
    if (xhr.status >= 500) opts.requestId = header(xhr, "X-Request-ID");
    tlmToast(messageFor(xhr), opts);
  });

  document.addEventListener("htmx:sendError", function () {
    showBanner();
  });

  document.addEventListener("htmx:timeout", function (e) {
    if (isPoll(e.detail && e.detail.elt)) {
      showBanner();
      return;
    }
    tlmToast(
      "The server took too long to respond. The action may still complete; " +
        "reload to check before trying again.",
      { kind: "error" }
    );
  });

  document.addEventListener("htmx:afterRequest", function (e) {
    var xhr = e.detail && e.detail.xhr;
    // Any HTTP status, even an error, proves the server is reachable again.
    if (xhr && xhr.status > 0) hideBanner();
  });

  window.tlmToast = tlmToast;
})();
