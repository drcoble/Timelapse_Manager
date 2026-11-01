/*
 * Polling policy for background refreshes (#70 F-S1, F-P3; WP-70-E1).
 *
 * A background poll is not user activity. Before this module every poll
 * refreshed the session's idle clock, so a dashboard left open never signed
 * out, and every poll kept running in hidden tabs, piled up behind a hung
 * server and retried at full rate after errors.
 *
 * A poller is either an htmx element carrying `data-poll` (the alerts badge,
 * the dashboard grid, the renders list) or a fetch poller registered with
 * `window.tlmPoll.every` (the new-frames and new-events pills).
 *
 *   - Activity: pointerdown, keydown, wheel, scroll, touchstart and focusin
 *     (a keyboard or screen-reader user moving focus) record the time of the
 *     last input. Page load counts as input: the load itself was a request.
 *   - Passive marking: a request from a `data-poll` element itself (never a
 *     child: a card or drawer click inside the polled grid stays an ordinary
 *     request) and every tlmPoll request carry `X-TLM-Passive: 1` and
 *     `X-TLM-Idle: <whole seconds since the last input>`. The server refreshes
 *     the idle clock only when that idle time is short (or when
 *     `session.keepalive_on_poll` is on), so an idle page signs out.
 *     A `load`-triggered element inside a `data-poll` region is passive too
 *     (#278): a poll swap that brings in new cards brings in their lazy
 *     ribbons, whose `load` fetches would otherwise count as activity and keep
 *     an unattended dashboard signed in. The header carries the real idle time,
 *     so a request that really follows input (an element a click swapped in)
 *     is still counted by the server. Clicks and other triggers stay ordinary.
 *   - Paused: a `data-poll` request is cancelled while the tab is hidden or
 *     the element has `data-poll-paused` (an open panel the poll would
 *     disturb).
 *   - Version round-trip: a `data-poll` response that carries
 *     `X-TLM-Poll-Version` stores it on the element as `data-poll-version`
 *     (a page can seed it there too), and the element's next poll sends it
 *     back. A server that sees its current version answers 204, which htmx
 *     does not swap, so an unchanged region costs no render and no swap. A
 *     response without the header (a 204 may omit it) keeps the stored value.
 *   - Back to visible: `tlm:visible` is dispatched on `document` (htmx pollers
 *     list `tlm:visible from:document` in `hx-trigger`), and every fetch
 *     poller runs once at once, so a tab switched back to is current.
 *
 * window.tlmPoll.every(name, fn, ms)
 *     Run `fn(request)` every `ms` milliseconds, replacing any poller already
 *     registered under `name`. `request(url)` fetches `url` as a passive poll
 *     and resolves with the parsed JSON (null for an empty or non-JSON body);
 *     `fn` returns its promise. Skipped while hidden; never two in flight;
 *     aborted after 15 s; after a failure the delay doubles (capped at 5 min)
 *     until a success resets it. A 401 stops the poller and sends the browser
 *     to /login with the current page as `next`.
 *
 * window.tlmPoll.stop(name)
 *     Stop the poller registered under `name`, if any.
 */
(function () {
  "use strict";

  var REQUEST_TIMEOUT_MS = 15000;
  var MAX_DELAY_MS = 5 * 60 * 1000;
  var INPUT_EVENTS = ["pointerdown", "keydown", "wheel", "scroll", "touchstart", "focusin"];
  var VERSION_HEADER = "X-TLM-Poll-Version";
  var VERSION_ATTR = "data-poll-version";

  // ---- (a) activity tracker -------------------------------------------------

  var lastInput = Date.now();

  function noteInput() {
    lastInput = Date.now();
  }

  // Capture phase on window, so a handler that stops propagation cannot hide
  // input from the tracker.
  for (var i = 0; i < INPUT_EVENTS.length; i++) {
    window.addEventListener(INPUT_EVENTS[i], noteInput, { capture: true, passive: true });
  }

  function idleSeconds() {
    return Math.max(0, Math.floor((Date.now() - lastInput) / 1000));
  }

  function passiveHeaders() {
    return { "X-TLM-Passive": "1", "X-TLM-Idle": String(idleSeconds()) };
  }

  function isPoller(elt) {
    return !!(elt && elt.hasAttribute && elt.hasAttribute("data-poll"));
  }

  var LOAD_TRIGGER = /(^|[\s,])load(\s|,|\[|$)/;

  // An element that fetches itself on `load` from inside a polled region: the
  // content a poll swap just inserted (never the poller itself, never a click).
  function isLoadInsidePoller(elt) {
    if (!elt || !elt.getAttribute || !elt.parentElement) return false;
    var trigger = elt.getAttribute("hx-trigger");
    if (!trigger || !LOAD_TRIGGER.test(trigger)) return false;
    return !!elt.parentElement.closest("[data-poll]");
  }

  // ---- (b) passive marking for htmx pollers --------------------------------
  // Set per request rather than with hx-headers: hx-headers is inherited, so
  // every child request of a polled container would go passive too.

  document.addEventListener("htmx:configRequest", function (e) {
    var d = e.detail || {};
    if (!d.headers) return;
    var poller = isPoller(d.elt);
    if (!poller && !isLoadInsidePoller(d.elt)) return;
    var h = passiveHeaders();
    for (var k in h) d.headers[k] = h[k];
    if (!poller) return; // the version round-trip is the poller's own
    var version = d.elt.getAttribute(VERSION_ATTR);
    if (version) d.headers[VERSION_HEADER] = version;
  });

  // Keep the version the server sent with the poller's last response.
  document.addEventListener("htmx:afterRequest", function (e) {
    var d = e.detail || {};
    if (!isPoller(d.elt) || !d.xhr || !d.successful) return;
    var version = d.xhr.getResponseHeader(VERSION_HEADER);
    if (version) d.elt.setAttribute(VERSION_ATTR, version);
  });

  // ---- (c) paused while hidden or flagged ----------------------------------

  document.addEventListener("htmx:beforeRequest", function (e) {
    var elt = e.detail && e.detail.elt;
    if (!isPoller(elt)) return;
    if (document.hidden || elt.hasAttribute("data-poll-paused")) e.preventDefault();
  });

  // ---- (e) fetch pollers ----------------------------------------------------

  var pollers = Object.create(null);

  function loginUrl() {
    return "/login?next=" + encodeURIComponent(window.location.pathname + window.location.search);
  }

  function PollStop() {}

  function makeRequest(state) {
    return function request(url) {
      var controller = typeof AbortController === "function" ? new AbortController() : null;
      // A plain setTimeout, not AbortSignal.timeout(): the timer stays visible
      // to (and controllable by) fake clocks in the browser tests.
      var timer = controller
        ? window.setTimeout(function () { controller.abort(); }, REQUEST_TIMEOUT_MS)
        : null;
      var headers = passiveHeaders();
      headers["X-Requested-With"] = "fetch";
      headers.Accept = "application/json";
      var opts = { headers: headers, credentials: "same-origin" };
      if (controller) opts.signal = controller.signal;
      return window.fetch(url, opts).then(
        function (r) {
          if (timer) window.clearTimeout(timer);
          if (r.status === 401) {
            stopState(state);
            window.location.assign(loginUrl());
            throw new PollStop();
          }
          if (!r.ok) throw new Error("HTTP " + r.status);
          return r.text().then(function (text) {
            if (!text) return null;
            try {
              return JSON.parse(text);
            } catch (err) {
              return null;
            }
          });
        },
        function (err) {
          if (timer) window.clearTimeout(timer);
          throw err;
        }
      );
    };
  }

  function stopState(state) {
    state.stopped = true;
    if (state.timer) window.clearTimeout(state.timer);
    state.timer = null;
    if (pollers[state.name] === state) delete pollers[state.name];
  }

  function nextDelay(state) {
    return Math.min(state.ms * Math.pow(2, state.failures), MAX_DELAY_MS);
  }

  function schedule(state) {
    if (state.stopped) return;
    if (state.timer) window.clearTimeout(state.timer);
    state.timer = window.setTimeout(function () {
      state.timer = null;
      run(state, true);
    }, nextDelay(state));
  }

  // One tick. `reschedule` is false for the extra run on becoming visible,
  // which leaves the existing schedule alone.
  function run(state, reschedule) {
    if (state.stopped) return;
    if (document.hidden || state.inFlight) {
      if (reschedule) schedule(state);
      return;
    }
    state.inFlight = true;
    var p;
    try {
      p = Promise.resolve(state.fn(state.request));
    } catch (err) {
      p = Promise.reject(err);
    }
    p.then(
      function () {
        state.failures = 0;
      },
      function (err) {
        if (!(err instanceof PollStop)) state.failures += 1;
      }
    ).then(function () {
      state.inFlight = false;
      if (reschedule) schedule(state);
    });
  }

  function every(name, fn, ms) {
    stop(name);
    var state = {
      name: name,
      fn: fn,
      ms: ms,
      failures: 0,
      inFlight: false,
      stopped: false,
      timer: null,
      request: null
    };
    state.request = makeRequest(state);
    pollers[name] = state;
    schedule(state);
  }

  function stop(name) {
    var state = pollers[name];
    if (state) stopState(state);
  }

  // ---- (d) back to visible --------------------------------------------------

  document.addEventListener("visibilitychange", function () {
    if (document.hidden) return;
    document.dispatchEvent(new Event("tlm:visible"));
    for (var name in pollers) run(pollers[name], false);
  });

  window.tlmPoll = { every: every, stop: stop };
})();
