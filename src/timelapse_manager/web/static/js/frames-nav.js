/*
 * Frames-browser time navigation.
 *
 * Three independent, progressively-enhanced behaviours on the single-project
 * frames page. Each no-ops gracefully when its markup is absent (the
 * All-Projects grid hides the ribbon and the jump form), and all are tolerant
 * of HTMX-swapped grid content.
 *
 *  1. Ribbon jump — a `ribbon:jump` event (dispatched by ribbon.js when the
 *     interactive ribbon is clicked) navigates the grid to `?at=<iso>` via an
 *     HTMX GET, resetting the grid to a window centered on that time.
 *
 *  2. Scroll-position cursor — an IntersectionObserver tracks which tiles are
 *     visible and moves a 1px accent line on the ribbon to the newest visible
 *     tile's capture time, so the ribbon reflects the scroll position. The
 *     observer is (re)built after every grid swap.
 *
 *  3. New-frames pill — polls /frames/since on a ~30s cadence and reveals a
 *     "N new frames" pill only on a 0->N transition (so assistive tech is not
 *     re-announced every tick). Refresh reloads the grid and the baseline resets
 *     from the new grid's newest id.
 */
(function () {
  "use strict";

  var POLL_MS = 30000;
  var JUMP_DEBOUNCE_MS = 120;

  function grid() {
    return document.getElementById("frame-grid");
  }

  // --- 1. Ribbon jump ------------------------------------------------------
  // A click on the interactive ribbon resets the grid to a window centered on
  // the clicked time. ribbon.js gives us epoch milliseconds.
  var jumpTimer = null;
  document.addEventListener("ribbon:jump", function (e) {
    var g = grid();
    if (!g || !e.detail || typeof e.detail.timestampMs !== "number") return;
    var ribbon = document.querySelector(".frame-ribbon");
    var projectId = ribbon ? ribbon.getAttribute("data-project-id") : null;
    if (!projectId) {
      // Fall back to the jump form's hidden project_id.
      var hidden = document.querySelector(
        ".frame-jump input[name='project_id']"
      );
      projectId = hidden ? hidden.value : null;
    }
    if (!projectId) return;
    var iso = new Date(e.detail.timestampMs).toISOString();
    if (jumpTimer) window.clearTimeout(jumpTimer);
    jumpTimer = window.setTimeout(function () {
      if (!window.htmx) return;
      var url =
        "/frames?project_id=" +
        encodeURIComponent(projectId) +
        "&at=" +
        encodeURIComponent(iso);
      var showDeleted = document.querySelector(
        ".frame-jump input[name='show_deleted']"
      );
      if (showDeleted) url += "&show_deleted=1";
      window.htmx.ajax("GET", url, {
        target: "#frame-grid",
        swap: "innerHTML",
      });
    }, JUMP_DEBOUNCE_MS);
  });

  // --- 2. Scroll-position cursor -------------------------------------------
  var observer = null;

  function ribbonBounds() {
    var wrap = document.querySelector(".frame-ribbon .time-ribbon");
    if (!wrap) return null;
    var start = parseInt(wrap.getAttribute("data-start"), 10);
    var end = parseInt(wrap.getAttribute("data-end"), 10);
    if (isNaN(start) || isNaN(end) || end <= start) return null;
    return { wrap: wrap, start: start, end: end };
  }

  function ensureCursor(wrap) {
    var cursor = wrap.querySelector(".ribbon-scroll-cursor");
    if (!cursor) {
      cursor = document.createElement("div");
      cursor.className = "ribbon-scroll-cursor";
      cursor.setAttribute("aria-hidden", "true");
      wrap.appendChild(cursor);
    }
    return cursor;
  }

  function placeCursor(epochSeconds) {
    var b = ribbonBounds();
    if (!b) return;
    var fraction = (epochSeconds - b.start) / (b.end - b.start);
    if (fraction < 0) fraction = 0;
    if (fraction > 1) fraction = 1;
    var cursor = ensureCursor(b.wrap);
    cursor.style.left = fraction * 100 + "%";
  }

  // The cursor reflects the CURRENT scroll position, so track the live set of
  // on-screen tiles (add on enter, remove on exit) and place the cursor at the
  // newest still-visible capture time. A running max would latch at the newest
  // tile and never move left as the user scrolls toward older frames.
  var visibleTiles = new Map(); // tile element -> capture epoch seconds
  var cursorTimer = null;
  function onIntersect(entries) {
    for (var i = 0; i < entries.length; i++) {
      var el = entries[i].target;
      var ts = parseInt(el.getAttribute("data-timestamp"), 10);
      if (isNaN(ts)) continue;
      if (entries[i].isIntersecting) {
        visibleTiles.set(el, ts);
      } else {
        visibleTiles.delete(el);
      }
    }
    // Debounced so a burst of callbacks during a scroll resolves to one move.
    if (cursorTimer) window.clearTimeout(cursorTimer);
    cursorTimer = window.setTimeout(reflectVisible, JUMP_DEBOUNCE_MS);
  }

  // Move the scroll cursor to the newest visible tile and hand the loaded
  // window's [oldest, newest] span to the scrubber (which owns the viewport
  // rect, in a separate IIFE) via an event. Called on intersect changes and
  // once more when the ribbon settles (so the rect paints on first load even if
  // the SVG arrives after the initial observer pass).
  function reflectVisible() {
    var newest = null;
    var oldest = null;
    visibleTiles.forEach(function (ts) {
      if (newest === null || ts > newest) newest = ts;
      if (oldest === null || ts < oldest) oldest = ts;
    });
    if (newest !== null) placeCursor(newest);
    if (newest !== null && oldest !== null) {
      document.dispatchEvent(
        new CustomEvent("scrubber:viewport", {
          detail: { newest: newest, oldest: oldest },
        })
      );
    }
  }

  function rebuildObserver() {
    if (!("IntersectionObserver" in window)) return;
    if (!ribbonBounds()) return; // no ribbon (All-Projects) -> nothing to track
    var g = grid();
    if (!g) return;
    if (observer) observer.disconnect();
    visibleTiles.clear();
    observer = new IntersectionObserver(onIntersect, { threshold: 0.1 });
    var tiles = g.querySelectorAll(".frame-tile[data-timestamp]");
    for (var i = 0; i < tiles.length; i++) observer.observe(tiles[i]);
  }

  // --- 3. New-frames pill --------------------------------------------------
  var pollTimer = null;
  var lastCount = 0;

  function pill() {
    return document.getElementById("frame-new-pill");
  }

  function newestGridId() {
    // The sentinel/end-cap carries the grid's newest frame id.
    var marker = document.querySelector(
      "#frame-grid .frame-sentinel, #frame-grid .frame-end-cap"
    );
    if (!marker) return null;
    var id = parseInt(marker.getAttribute("data-newest-id"), 10);
    return isNaN(id) ? null : id;
  }

  function poll() {
    var p = pill();
    if (!p || !window.fetch) return;
    var after = newestGridId();
    if (after === null) return;
    var url = "/frames/since?after=" + encodeURIComponent(after);
    var projectId = p.getAttribute("data-project-id");
    if (projectId) url += "&project_id=" + encodeURIComponent(projectId);
    if (p.getAttribute("data-show-deleted")) url += "&show_deleted=1";
    window
      .fetch(url, { headers: { "X-Requested-With": "fetch" } })
      .then(function (r) {
        return r.ok ? r.json() : null;
      })
      .then(function (data) {
        if (!data || typeof data.count !== "number") return;
        var n = data.count;
        // Announce only on a 0->N edge, not on every tick that still has N.
        if (n > 0 && lastCount === 0) {
          var label = n === 1 ? "1 new frame" : n + " new frames";
          var span = p.querySelector("[data-new-count]");
          if (span) span.textContent = label;
          p.hidden = false;
        } else if (n === 0) {
          p.hidden = true;
        }
        lastCount = n;
      })
      .catch(function () {
        /* transient network error -> retry next tick */
      });
  }

  function startPolling() {
    if (pollTimer) window.clearInterval(pollTimer);
    if (!pill()) return;
    pollTimer = window.setInterval(poll, POLL_MS);
  }

  function resetBaseline() {
    // After a refresh (or any full grid reload) the newest id changed, so clear
    // the pill and re-baseline from the new grid.
    lastCount = 0;
    var p = pill();
    if (p) p.hidden = true;
  }

  // --- Wiring --------------------------------------------------------------
  function init() {
    rebuildObserver();
    startPolling();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  // Re-init after the grid (or ribbon) swaps in new content.
  document.body.addEventListener("htmx:afterSettle", function (e) {
    var t = e.target;
    if (!t) return;
    if (t.id === "frame-grid" || (t.closest && t.closest("#frame-grid"))) {
      rebuildObserver();
    }
    if (t.classList && t.classList.contains("frame-ribbon")) {
      rebuildObserver(); // ribbon arrived late -> attach the cursor now
      reflectVisible(); // and paint the cursor + viewport rect immediately
    }
  });

  // A full grid reload (the Refresh button, a date jump, or a Start/Newest/gap
  // jump) changes the newest id, so the pill baseline resets here. It also moves
  // keyboard focus into the freshly loaded window so a jump lands the user on the
  // first frame rather than leaving focus on the (now off-screen) control.
  document.body.addEventListener("htmx:afterSwap", function (e) {
    if (e.target && e.target.id === "frame-grid") {
      resetBaseline();
      var firstThumb = e.target.querySelector(".frame-thumb");
      if (firstThumb && typeof firstThumb.focus === "function") {
        firstThumb.focus();
      }
    }
  });
})();
