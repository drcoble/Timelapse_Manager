/*
 * Guarded fetch for JS-driven requests (#70 F-R2).
 *
 * The frames page drives a few JSON routes from script (offset, undo, export,
 * the range count) with raw fetch. Before this module a double click ran the
 * action twice, a non-JSON error body (the CSRF 403 is plain text) threw inside
 * `r.json()`, and failures were silent. Every helper here is keyed by a short
 * action name:
 *
 *   tlmExclusive(key, trigger, fn)
 *       Run `fn()` (which returns a promise) while holding `key`. A second call
 *       while the key is held returns null and does nothing; `trigger` (a
 *       button) is disabled until the promise settles. Use it around a whole
 *       user action, including a multi-step one (materialize, then offset).
 *
 *   tlmRequest(key, {url, method, body, headers, trigger, passive, timeoutMs,
 *                    idempotent, idempotencyKey, supersede, quiet})
 *       One request. Re-entry on a held key returns null (or, with
 *       `supersede: true`, aborts the older request so the newest wins). It
 *       reads the CSRF token from <meta name="csrf-token"> at call time, sends
 *       `X-Requested-With: fetch`, sends `Idempotency-Key` when `idempotent`
 *       (minted per call unless `idempotencyKey` is given), sends
 *       `X-TLM-Passive: 1` when `passive`, and aborts after `timeoutMs`
 *       (default 30 s). It resolves with the parsed JSON body (null when the
 *       body is empty or not JSON) and rejects with
 *       {status, detail, aborted, superseded}. A failure raises a toast through
 *       window.tlmToast (htmx-errors.js) unless `quiet`; a superseded request
 *       never does.
 *
 *   tlmIdempotencyKey()
 *       A fresh random key. crypto.randomUUID only exists in a secure context,
 *       and direct http://<vm>:8080 access is not one, so the fallback builds a
 *       v4 UUID from crypto.getRandomValues.
 */
(function () {
  "use strict";

  var DEFAULT_TIMEOUT_MS = 30000;
  var CSRF_EVENT = "tlm:csrf-failed";
  var CSRF_BODY = "CSRF validation failed.";
  var DUPLICATE_DETAIL = "duplicate request in progress";

  var STATUS_MESSAGES = {
    401: "Your session has ended. Sign in again.",
    403: "You do not have permission to do that.",
    404: "That item no longer exists.",
    409: "That conflicts with a change made elsewhere. Reload and try again.",
    413: "Request too large.",
    429: "Too many requests. Wait a moment and try again."
  };

  // key -> {controller} for requests and true for exclusive actions.
  var held = Object.create(null);

  function toast(message, opts) {
    if (typeof window.tlmToast === "function") window.tlmToast(message, opts || {});
  }

  function csrfToken() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute("content") || "" : "";
  }

  function hex(bytes) {
    var out = "";
    for (var i = 0; i < bytes.length; i++) {
      out += (bytes[i] + 0x100).toString(16).slice(1);
    }
    return out;
  }

  function tlmIdempotencyKey() {
    var c = window.crypto;
    if (c && typeof c.randomUUID === "function") return c.randomUUID();
    var b = new Uint8Array(16);
    c.getRandomValues(b);
    b[6] = (b[6] & 0x0f) | 0x40; // version 4
    b[8] = (b[8] & 0x3f) | 0x80; // RFC 4122 variant
    var h = hex(b);
    return (
      h.slice(0, 8) + "-" + h.slice(8, 12) + "-" + h.slice(12, 16) + "-" +
      h.slice(16, 20) + "-" + h.slice(20)
    );
  }

  // `data-tlm-busy` marks what this module disabled, so a markup snapshot taken
  // mid-request (selection.js keeps one of the action bar) can undo it.
  function setBusy(trigger, busy) {
    if (!trigger) return;
    if (busy) {
      trigger.disabled = true;
      trigger.setAttribute("aria-busy", "true");
      trigger.setAttribute("data-tlm-busy", "");
    } else {
      trigger.disabled = false;
      trigger.removeAttribute("aria-busy");
      trigger.removeAttribute("data-tlm-busy");
    }
  }

  function tlmExclusive(key, trigger, fn) {
    if (held[key]) return null;
    held[key] = true;
    setBusy(trigger, true);
    function done() {
      delete held[key];
      setBusy(trigger, false);
    }
    var p;
    try {
      p = Promise.resolve(fn());
    } catch (err) {
      done();
      return Promise.reject(err);
    }
    return p.then(
      function (v) { done(); return v; },
      function (err) { done(); throw err; }
    );
  }

  function encodeBody(body) {
    if (body == null || typeof body === "string") return body;
    if (body instanceof FormData || body instanceof URLSearchParams) return body;
    return Object.keys(body)
      .map(function (k) {
        return encodeURIComponent(k) + "=" + encodeURIComponent(body[k]);
      })
      .join("&");
  }

  // Parse a response body defensively: JSON when it is JSON, text otherwise.
  function readBody(r) {
    return r.text().then(
      function (text) {
        var type = r.headers.get("Content-Type") || "";
        if (text && type.indexOf("json") !== -1) {
          try {
            return { json: JSON.parse(text), text: text };
          } catch (e) {
            return { json: null, text: text };
          }
        }
        return { json: null, text: text };
      },
      function () {
        return { json: null, text: "" };
      }
    );
  }

  function detailOf(status, parsed) {
    var j = parsed.json;
    if (j && typeof j.detail === "string" && j.detail) return j.detail;
    if (j && typeof j.error === "string" && j.error) return j.error;
    if (status >= 500) return "Server error";
    var text = (parsed.text || "").trim();
    // A short plain-text body is a message (the CSRF 403); HTML is not.
    if (text && text.length <= 200 && text.charAt(0) !== "<") return text;
    return STATUS_MESSAGES[status] || "Request failed (HTTP " + status + ").";
  }

  function reportFailure(err, r) {
    // The server's idempotency guard saw this action already running (another
    // tab, or a retry racing the original); the original reports its own result.
    if (err.status === 409 && err.detail === DUPLICATE_DETAIL) return;
    if (err.status === 403 && err.detail === CSRF_BODY) {
      // Same toast htmx-errors.js shows for an htmx request's CSRF 403.
      document.dispatchEvent(new CustomEvent(CSRF_EVENT));
      return;
    }
    if (err.status === 0) {
      toast(
        err.aborted
          ? "The server took too long to respond. The action may still complete; " +
            "reload to check before trying again."
          : "Connection lost. Check the connection and try again.",
        { kind: "error" }
      );
      return;
    }
    var opts = { kind: "error" };
    var msg = err.detail;
    if (err.status >= 500) {
      var rid = r && r.headers.get("X-Request-ID");
      if (rid) {
        opts.requestId = rid;
        msg = "Server error (request id " + rid + ")";
      }
    }
    toast(msg, opts);
  }

  function tlmRequest(key, opts) {
    opts = opts || {};
    var prior = held[key];
    if (prior) {
      if (!opts.supersede || !prior.controller) return null;
      prior.superseded = true;
      prior.controller.abort();
    }
    var controller = typeof AbortController === "function" ? new AbortController() : null;
    var slot = { controller: controller, superseded: false, timedOut: false };
    held[key] = slot;
    setBusy(opts.trigger, true);

    var method = (opts.method || "GET").toUpperCase();
    var headers = { "X-Requested-With": "fetch", Accept: "application/json" };
    if (method !== "GET" && method !== "HEAD") {
      headers["X-CSRF-Token"] = csrfToken();
      // FormData sets its own multipart boundary; everything else is a form body.
      if (opts.body != null && !(opts.body instanceof FormData)) {
        headers["Content-Type"] = "application/x-www-form-urlencoded";
      }
    }
    if (opts.idempotent) {
      headers["Idempotency-Key"] = opts.idempotencyKey || tlmIdempotencyKey();
    }
    if (opts.passive) headers["X-TLM-Passive"] = "1";
    if (opts.headers) {
      Object.keys(opts.headers).forEach(function (h) { headers[h] = opts.headers[h]; });
    }

    var timeoutMs = typeof opts.timeoutMs === "number" ? opts.timeoutMs : DEFAULT_TIMEOUT_MS;
    var timer = null;
    if (controller && timeoutMs > 0) {
      timer = window.setTimeout(function () {
        slot.timedOut = true;
        controller.abort();
      }, timeoutMs);
    }

    function release() {
      if (timer) window.clearTimeout(timer);
      if (held[key] === slot) {
        delete held[key];
        setBusy(opts.trigger, false);
      }
    }

    function fail(err, r) {
      release();
      if (!err.superseded && !opts.quiet) reportFailure(err, r);
      throw err;
    }

    var init = {
      method: method,
      headers: headers,
      credentials: "same-origin",
      body: encodeBody(opts.body)
    };
    if (controller) init.signal = controller.signal;

    return fetch(opts.url, init).then(
      function (r) {
        return readBody(r).then(function (parsed) {
          if (!r.ok) {
            return fail({ status: r.status, detail: detailOf(r.status, parsed) }, r);
          }
          release();
          return parsed.json;
        });
      },
      function () {
        var err = {
          status: 0,
          detail: slot.timedOut ? "timeout" : "network",
          aborted: slot.timedOut,
          superseded: slot.superseded
        };
        return fail(err, null);
      }
    );
  }

  tlmRequest.isHeld = function (key) {
    return !!held[key];
  };

  window.tlmRequest = tlmRequest;
  window.tlmExclusive = tlmExclusive;
  window.tlmIdempotencyKey = tlmIdempotencyKey;
})();
