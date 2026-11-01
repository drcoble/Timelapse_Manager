/*
 * Operational-events log time navigation.
 *
 * Three independent, progressively-enhanced behaviours on the events page. Each
 * no-ops gracefully when its markup is absent, and all tolerate HTMX-swapped
 * list content (the table body is replaced on filter/jump/refresh).
 *
 *  1. Level chips — a multi-select chip group (all pressed = all levels). chips.js
 *     toggles each chip's aria-pressed; this module reads the pressed set after a
 *     toggle, mirrors it into the filter form's hidden `level` input, and re-queries
 *     the list (GET /events/batch -> #events-tbody).
 *
 *  2. Date/time jump — the form's hx-get carries the live level selection (set
 *     on the request in an htmx:configRequest listener) and windows the list at
 *     the chosen instant (?at=<iso>); the server swaps the batch into #events-tbody.
 *     No JS: the form is a plain GET with a hidden `level` mirror. (Not an
 *     hx-vals "js:" hook: htmx evaluates those with Function(), which the CSP's
 *     script-src, without 'unsafe-eval', refuses -- the jump never fired, #34.)
 *
 *  3. New-events pill — polls /events/since on a ~30s cadence (with the active
 *     filters) and reveals a "N new events" pill only on a 0->N transition. Refresh
 *     reloads the list and the baseline resets from the new list's newest id.
 *     The poll runs through tlmPoll (poll.js): passive, paused while the tab is
 *     hidden, with backoff after errors.
 */
(function () {
  "use strict";

  var POLL_MS = 30000;
  var ALL_LEVELS = ["info", "warning", "error", "critical"];

  function chipGroup() {
    return document.getElementById("events-level-chips");
  }

  // The pressed level chips as a comma-separated backend value. An empty string
  // means "no filter" (all levels) -- which is also what the backend treats an
  // absent/empty `level` as, so all-pressed and none-pressed both show everything.
  function activeLevels() {
    var group = chipGroup();
    if (!group) return "";
    var pressed = [];
    ALL_LEVELS.forEach(function (lv) {
      var chip = group.querySelector('.chip[data-level="' + lv + '"]');
      if (chip && chip.getAttribute("aria-pressed") === "true") pressed.push(lv);
    });
    // All four pressed is equivalent to no filter; send empty so the URL stays
    // clean and the "all" case is unambiguous.
    if (pressed.length === ALL_LEVELS.length) return "";
    return pressed.join(",");
  }


  function syncHiddenLevel() {
    var input = document.getElementById("events-level-input");
    if (input) input.value = activeLevels();
  }

  // The active level/q/scope query the visible list is filtered by. Shared by
  // the chip re-query URL and the since-poll URL so both reflect exactly what
  // the user sees.
  function activeFilterQuery() {
    var params = [];
    var levels = activeLevels();
    if (levels) params.push("level=" + encodeURIComponent(levels));
    var q = document.getElementById("q");
    if (q && q.value.trim()) params.push("q=" + encodeURIComponent(q.value.trim()));
    var scope = document.getElementById("scope");
    if (scope && scope.value) params.push("scope=" + encodeURIComponent(scope.value));
    return params.join("&");
  }

  // The list re-query URL built from the live filter state. Used for chip
  // re-filtering so the request reflects the final selection rather than racing
  // the form's hidden-input serialization. It is the batch fragment route, like
  // the Refresh button: /events answers a non-jump request with the whole page,
  // which swapped into #events-tbody nested a second page (duplicate chips and
  // ids, scripts run again so a later chip click toggled twice, #34).
  function listQueryUrl() {
    var filters = activeFilterQuery();
    return "/events/batch" + (filters ? "?" + filters : "");
  }

  // --- 1. Level chips ------------------------------------------------------
  // chips.js toggles aria-pressed on click (it is registered first, so it has
  // already flipped the state by the time this handler runs). Here we sync the
  // hidden input (for the no-JS form) and re-query the list with the new
  // selection. The re-query is debounced so a burst of chip clicks collapses
  // into a single request built from the final pressed set.
  var refilterTimer = null;
  document.addEventListener("click", function (e) {
    if (!e.target || typeof e.target.closest !== "function") return;
    var chip = e.target.closest("#events-level-chips .chip[aria-pressed]");
    if (!chip) return;
    syncHiddenLevel();
    if (!window.htmx || typeof window.htmx.ajax !== "function") return;
    if (refilterTimer) window.clearTimeout(refilterTimer);
    refilterTimer = window.setTimeout(function () {
      window.htmx.ajax("GET", listQueryUrl(), {
        target: "#events-tbody",
        swap: "innerHTML",
      });
    }, 150);
  });

  // --- 2. Date/time jump ---------------------------------------------------
  // The jump request carries the live chip selection, overriding the hidden
  // no-JS `level` mirror the form serialises.
  document.addEventListener("htmx:configRequest", function (e) {
    var elt = e.detail && e.detail.elt;
    if (!elt || elt.id !== "events-jump") return;
    e.detail.parameters.level = activeLevels();
  });

  // --- 3. New-events pill --------------------------------------------------
  var POLL_NAME = "events-since";
  var lastCount = 0;

  function pill() {
    return document.getElementById("events-new-pill");
  }

  function listBody() {
    return document.getElementById("events-tbody");
  }

  function newestListId() {
    // The sentinel/end-cap row carries the list's newest event id.
    var body = listBody();
    if (!body) return null;
    var marker = body.querySelector(".log-sentinel, .log-end-cap");
    if (!marker) return null;
    var id = parseInt(marker.getAttribute("data-newest-id"), 10);
    return isNaN(id) ? null : id;
  }

  // A tlmPoll (poll.js) callback: `request` sends the passive-poll headers and
  // rejects on an error, which tlmPoll answers with backoff.
  function poll(request) {
    var p = pill();
    if (!p || !window.fetch) return;
    var after = newestListId();
    if (after === null) return;
    var url = "/events/since?after=" + encodeURIComponent(after);
    var filters = activeFilterQuery();
    if (filters) url += "&" + filters;
    return request(url).then(function (data) {
      if (!data || typeof data.count !== "number") return;
      var n = data.count;
      // Announce only on a 0->N edge, not on every tick that still has N.
      if (n > 0 && lastCount === 0) {
        var label = n === 1 ? "1 new event" : n + " new events";
        var span = p.querySelector("[data-new-count]");
        if (span) span.textContent = label;
        p.hidden = false;
      } else if (n === 0) {
        p.hidden = true;
      }
      lastCount = n;
    });
  }

  function startPolling() {
    if (!window.tlmPoll) return;
    if (!pill()) {
      window.tlmPoll.stop(POLL_NAME);
      return;
    }
    window.tlmPoll.every(POLL_NAME, poll, POLL_MS);
  }

  function resetBaseline() {
    // After a refresh (or any full list reload) the newest id changed, so clear
    // the pill and re-baseline from the new list.
    lastCount = 0;
    var p = pill();
    if (p) p.hidden = true;
  }

  // --- Wiring --------------------------------------------------------------
  function init() {
    syncHiddenLevel();
    startPolling();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  // A full list reload (the Refresh button, a chip re-filter, or a date jump)
  // changes the newest id, so the pill baseline resets here.
  document.body.addEventListener("htmx:afterSwap", function (e) {
    if (e.target && e.target.id === "events-tbody") resetBaseline();
  });
})();
