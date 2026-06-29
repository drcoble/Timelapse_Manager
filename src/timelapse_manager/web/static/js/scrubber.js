/*
 * Frames-browser scrubber.
 *
 * Promotes the interactive time-ribbon to a keyboard-operable navigation slider
 * and overlays a "you are here" viewport rectangle. Companion to frames-nav.js:
 * that module owns the scroll-cursor and the click->`?at=` jump (via ribbon.js's
 * `ribbon:jump`); this module adds the reflect-side viewport rect, a drag
 * preview tooltip, the `role="slider"` keyboard path, and the click->date-jump
 * pre-fill. Every behaviour no-ops gracefully when its markup is absent
 * (All-Projects hides the ribbon) and tolerates HTMX-swapped ribbon content.
 *
 * Reflect vs. drive: the viewport rect and the drag tooltip only REFLECT (pure
 * DOM, no requests); a click, Enter/Space, or arrow-step DRIVES exactly one
 * `?at=` load through the existing `ribbon:jump` path — so scroll, scrubber, and
 * date-jump never fight.
 */
(function () {
  "use strict";

  // One arrow-step moves this fraction of the whole span. The span can be
  // months wide, so a coarse step keeps keyboard travel usable; the date-jump
  // form remains the precision path.
  var KEY_STEP_FRACTION = 0.02;
  // A pointer movement under this many pixels is treated as a click, not a drag.
  var DRAG_THRESHOLD_PX = 4;

  function ribbonWrap() {
    // The slider control: the template wrapper, present from first paint and
    // stable across ribbon SVG swaps.
    return document.querySelector(".frame-ribbon");
  }

  // Epoch bounds live on the inner .time-ribbon, which arrives late via HTMX.
  // Returns null until it has loaded (callers must no-op gracefully).
  function bounds() {
    var inner = document.querySelector(".frame-ribbon .time-ribbon");
    if (!inner) return null;
    var start = parseInt(inner.getAttribute("data-start"), 10);
    var end = parseInt(inner.getAttribute("data-end"), 10);
    if (isNaN(start) || isNaN(end) || end <= start) return null;
    return { inner: inner, start: start, end: end };
  }

  function clampFraction(f) {
    return f < 0 ? 0 : f > 1 ? 1 : f;
  }

  function epochAtFraction(b, fraction) {
    return b.start + clampFraction(fraction) * (b.end - b.start);
  }

  // datetime-local inputs reject the `...:SS.sssZ` form, so trim to minutes.
  // The anchor is UTC (the server reads `at` as UTC-naive), so we slice the UTC
  // ISO string, NOT a viewer-tz rendering.
  function isoMinute(epochSeconds) {
    return new Date(epochSeconds * 1000).toISOString().slice(0, 16);
  }

  function isoFull(epochSeconds) {
    return new Date(epochSeconds * 1000).toISOString();
  }

  // --- Viewport "you are here" rect ---------------------------------------
  function ensureRect(inner) {
    var rect = inner.querySelector(".ribbon-viewport-rect");
    if (!rect) {
      rect = document.createElement("div");
      rect.className = "ribbon-viewport-rect";
      rect.setAttribute("aria-hidden", "true");
      inner.appendChild(rect);
    }
    return rect;
  }

  // Position the rect over the loaded window. Uses the SAME epoch->pixel mapping
  // as frames-nav.js's placeCursor (fraction = (ts - start) / (end - start)).
  function placeViewport(newestEpoch, oldestEpoch) {
    var b = bounds();
    if (!b) return;
    if (typeof newestEpoch !== "number" || typeof oldestEpoch !== "number") return;
    var lo = Math.min(newestEpoch, oldestEpoch);
    var hi = Math.max(newestEpoch, oldestEpoch);
    var left = clampFraction((lo - b.start) / (b.end - b.start));
    var right = clampFraction((hi - b.start) / (b.end - b.start));
    var rect = ensureRect(b.inner);
    rect.style.left = left * 100 + "%";
    rect.style.width = (right - left) * 100 + "%";
  }

  // frames-nav.js owns the visible-tile set; it hands us the loaded window's
  // newest+oldest epochs via this event so both IIFEs stay self-contained.
  document.addEventListener("scrubber:viewport", function (e) {
    if (!e.detail) return;
    placeViewport(e.detail.newest, e.detail.oldest);
    if (typeof e.detail.newest === "number" && typeof e.detail.oldest === "number") {
      lastViewportCenter = (e.detail.newest + e.detail.oldest) / 2;
      maybeUpdateZoom(lastViewportCenter);
    }
  });

  // --- Zoom strip ----------------------------------------------------------
  // For a long campaign the overview ribbon compresses each day to a sliver, so
  // a second, finer ribbon of the loaded window is lazy-loaded into the sibling
  // .frame-zoom-strip. It tracks the VIEWPORT (this same scrubber:viewport
  // event), matching the design's "finer ribbon of the window framed by the
  // viewport rect" — which uniformly covers initial load, jumps, and scroll.
  // Clicking the strip drives a jump for free: ribbon.js's delegated handler
  // reads the strip's own data-start/data-end (the window), so fine-resolution
  // clicks resolve to the right timestamp without any new wiring here.
  //
  // The strip is mouse-only by design (no role=slider / tabindex): keyboard
  // users navigate via the main ribbon slider, the date-jump form, and the
  // jump buttons, all of which already cover the full span at any precision.
  var ZOOM_THRESHOLD_DAYS = 60; // overview stays legible below this span
  var ZOOM_HALF_WINDOW_DAYS = 15; // → a 30-day magnified window
  var DAY_SECONDS = 86400;
  var zoomWindow = null; // {start, end} epoch secs currently loaded, or null
  var zoomLoading = false;
  // The last viewport centre, so the overview ribbon settling LATE than the grid
  // can still trigger the zoom load (the two load concurrently; either order).
  var lastViewportCenter = null;

  function zoomStrip() {
    return document.querySelector(".frame-zoom-strip");
  }

  function clearZoom() {
    var el = zoomStrip();
    if (el) {
      el.hidden = true;
      el.innerHTML = "";
    }
    zoomWindow = null;
  }

  function maybeUpdateZoom(centerEpoch) {
    var el = zoomStrip();
    if (!el || typeof centerEpoch !== "number") return;
    var b = bounds();
    if (!b) return;
    // Short campaigns need no magnification — tear any strip down.
    if ((b.end - b.start) / DAY_SECONDS <= ZOOM_THRESHOLD_DAYS) {
      if (zoomWindow) clearZoom();
      return;
    }
    var half = ZOOM_HALF_WINDOW_DAYS * DAY_SECONDS;
    // While the centre stays within the inner half of the loaded window, keep it
    // — only re-fetch once navigation carries you near/over an edge (no churn).
    if (zoomWindow) {
      var mid = (zoomWindow.start + zoomWindow.end) / 2;
      if (Math.abs(centerEpoch - mid) < half / 2) return;
    }
    if (zoomLoading || typeof htmx === "undefined") return;
    var wStart = Math.max(b.start, Math.round(centerEpoch - half));
    var wEnd = Math.min(b.end, Math.round(centerEpoch + half));
    if (wEnd <= wStart) return;
    var projectId = el.getAttribute("data-project-id");
    if (!projectId) return;
    var url =
      "/partials/projects/" +
      projectId +
      "/ribbon?h=36&decorative=1&window_start=" +
      wStart +
      "&window_end=" +
      wEnd;
    zoomLoading = true;
    htmx
      .ajax("GET", url, { target: el, swap: "innerHTML" })
      .then(function () {
        zoomWindow = { start: wStart, end: wEnd };
        el.hidden = false;
      })
      .catch(function () {
        /* leave the prior strip in place; retry on the next viewport change */
      })
      .finally(function () {
        zoomLoading = false;
      });
  }

  // --- Slider a11y ---------------------------------------------------------
  // role="slider" needs valid aria-value* attributes; they depend on the epoch
  // bounds, which load late. Populate (and refresh) them once the SVG settles.
  // aria-valuemin/max are epoch seconds; aria-valuenow tracks the slider thumb
  // position (the keyboard cursor), defaulting to the newest edge.
  var keyEpoch = null; // current keyboard position, epoch seconds

  function syncSliderAria() {
    var wrap = ribbonWrap();
    var b = bounds();
    if (!wrap || !b) return;
    if (keyEpoch === null) keyEpoch = b.end; // start at "now" / newest edge
    keyEpoch = Math.max(b.start, Math.min(b.end, keyEpoch));
    wrap.setAttribute("aria-valuemin", String(b.start));
    wrap.setAttribute("aria-valuemax", String(b.end));
    wrap.setAttribute("aria-valuenow", String(Math.round(keyEpoch)));
    wrap.setAttribute("aria-valuetext", isoFull(keyEpoch));
  }

  function commitJump(epochSeconds) {
    var wrap = ribbonWrap();
    if (!wrap) return;
    // Reuse the existing drive path: ribbon.js -> frames-nav.js turns this into
    // the single `?at=` grid load. Pre-fill the date-jump alongside (below).
    wrap.dispatchEvent(
      new CustomEvent("ribbon:jump", {
        detail: { timestampMs: Math.round(epochSeconds * 1000) },
        bubbles: true,
      })
    );
  }

  function prefillDateJump(epochSeconds) {
    var input = document.getElementById("frame-jump-at");
    if (input) input.value = isoMinute(epochSeconds);
  }

  function onSliderKey(e) {
    var wrap = ribbonWrap();
    if (!wrap || e.target !== wrap) return;
    var b = bounds();
    if (!b) return;
    var span = b.end - b.start;
    if (e.key === "ArrowRight" || e.key === "ArrowUp") {
      keyEpoch = Math.min(b.end, (keyEpoch === null ? b.end : keyEpoch) + span * KEY_STEP_FRACTION);
      syncSliderAria();
      e.preventDefault();
    } else if (e.key === "ArrowLeft" || e.key === "ArrowDown") {
      keyEpoch = Math.max(b.start, (keyEpoch === null ? b.end : keyEpoch) - span * KEY_STEP_FRACTION);
      syncSliderAria();
      e.preventDefault();
    } else if (e.key === "Home") {
      keyEpoch = b.start;
      syncSliderAria();
      e.preventDefault();
    } else if (e.key === "End") {
      keyEpoch = b.end;
      syncSliderAria();
      e.preventDefault();
    } else if (e.key === "Enter" || e.key === " ") {
      // commitJump dispatches ribbon:jump, which the prefill listener below
      // also handles — one drive path, one place that fills the date-jump.
      if (keyEpoch !== null) commitJump(keyEpoch);
      e.preventDefault();
    }
  }

  // --- Click -> date-jump pre-fill -----------------------------------------
  // ribbon.js already turns a ribbon click into the `?at=` jump (via
  // `ribbon:jump`). We ride the same gesture to ALSO pre-fill the date-jump so
  // the user can refine the minutes by keyboard. Listen on the jump event (not a
  // raw click) so the pixel->epoch math stays in one place (ribbon.js).
  document.addEventListener("ribbon:jump", function (e) {
    if (!e.detail || typeof e.detail.timestampMs !== "number") return;
    prefillDateJump(e.detail.timestampMs / 1000);
  });

  // --- Drag preview (tooltip) ----------------------------------------------
  // SCAFFOLD ONLY. A drag that ends ~where it started is a click and commits the
  // jump as today (we never swallow it). A real drag only PREVIEWS a tooltip and
  // is then swallowed so it does not fire a stray jump.
  //
  // Span-select: the down-point epoch and the live pointer epoch bound the
  // dragged range. On pointerup a genuine drag is turned into a RangeDescriptor
  // ({scope:"in_range", project_id, time_range:{from,to}, filters, deselected_ids})
  // covering min..max of the two epochs, the drag highlight is kept visible
  // (confirmed state), and POST /frames/range/count fetches the estimate, which is
  // handed with the descriptor to the selection spine for the escalation banner.
  var dragStartX = null;
  var dragStartEpoch = null;
  var dragCurrentEpoch = null;
  var dragMoved = false;
  var suppressNextClick = false;

  // The tooltip hangs off the .frame-ribbon wrapper, NOT the inner .time-ribbon:
  // the inner is overflow:hidden (it clips the SVG reveal wipe), which would clip
  // a tooltip rendered above the ribbon. Wrapper and inner share a width, so the
  // left-fraction mapping is identical.
  function ensureTooltip() {
    var wrap = ribbonWrap();
    if (!wrap) return null;
    var tip = wrap.querySelector(".scrubber-tooltip");
    if (!tip) {
      tip = document.createElement("div");
      tip.className = "scrubber-tooltip";
      tip.setAttribute("aria-hidden", "true");
      tip.hidden = true;
      wrap.appendChild(tip);
    }
    return tip;
  }

  function fractionFromEvent(inner, clientX) {
    var r = inner.getBoundingClientRect();
    if (!r.width) return null;
    return clampFraction((clientX - r.left) / r.width);
  }

  // --- Drag span highlight -------------------------------------------------
  // The shaded band over the dragged range. Lives on the inner .time-ribbon (it
  // is clipped to the track, unlike the tooltip which sits above it). Painted
  // during the drag and kept visible after pointerup as the confirmed selection.
  function ensureDragSpan(inner) {
    var span = inner.querySelector(".ribbon-drag-span");
    if (!span) {
      span = document.createElement("div");
      span.className = "ribbon-drag-span";
      span.setAttribute("aria-hidden", "true");
      inner.appendChild(span);
    }
    return span;
  }

  function clearDragSpan() {
    var b = bounds();
    var inner = b ? b.inner : document.querySelector(".frame-ribbon .time-ribbon");
    if (!inner) return;
    var span = inner.querySelector(".ribbon-drag-span");
    if (span) span.remove();
  }

  // Draw the span between two epochs as left/width percentages; `confirmed`
  // deepens the fill so a released selection reads as committed.
  function drawDragSpan(b, epochA, epochB, confirmed) {
    var lo = Math.min(epochA, epochB);
    var hi = Math.max(epochA, epochB);
    var left = clampFraction((lo - b.start) / (b.end - b.start));
    var right = clampFraction((hi - b.start) / (b.end - b.start));
    var span = ensureDragSpan(b.inner);
    span.style.left = left * 100 + "%";
    span.style.width = (right - left) * 100 + "%";
    if (confirmed) span.setAttribute("data-confirmed", "true");
    else span.removeAttribute("data-confirmed");
  }

  function onPointerDown(e) {
    // Clear any flag stranded by a prior drag whose synthetic click never
    // arrived (touch / large drags don't reliably emit one), so a stale flag
    // can't swallow this fresh gesture's click.
    suppressNextClick = false;
    if (e.button !== undefined && e.button !== 0) return; // primary only
    var inner = e.target && e.target.closest && e.target.closest(".frame-ribbon .time-ribbon");
    if (!inner) return;
    var b = bounds();
    if (!b) return;
    var f = fractionFromEvent(inner, e.clientX);
    if (f === null) return;
    dragStartX = e.clientX;
    dragStartEpoch = epochAtFraction(b, f);
    dragMoved = false;
    document.addEventListener("pointermove", onPointerMove);
    document.addEventListener("pointerup", onPointerUp);
  }

  function onPointerMove(e) {
    if (dragStartX === null) return;
    var b = bounds();
    var inner = b ? b.inner : null;
    if (!inner) return;
    if (Math.abs(e.clientX - dragStartX) > DRAG_THRESHOLD_PX) dragMoved = true;
    if (!dragMoved) return;
    var f = fractionFromEvent(inner, e.clientX);
    if (f === null) return;
    dragCurrentEpoch = epochAtFraction(b, f);
    var tip = ensureTooltip();
    if (tip) {
      tip.textContent = isoFull(dragCurrentEpoch);
      tip.style.left = f * 100 + "%";
      tip.hidden = false;
    }
    if (dragStartEpoch !== null) {
      drawDragSpan(b, dragStartEpoch, dragCurrentEpoch, false);
    }
  }

  function onPointerUp() {
    document.removeEventListener("pointermove", onPointerMove);
    document.removeEventListener("pointerup", onPointerUp);
    var wrap = ribbonWrap();
    if (wrap) {
      var tip = wrap.querySelector(".scrubber-tooltip");
      if (tip) tip.hidden = true;
    }
    if (dragMoved && dragStartEpoch !== null && dragCurrentEpoch !== null) {
      // A genuine drag selects a span. Swallow the trailing synthetic click so
      // ribbon.js does not also fire a stray jump, keep the highlight visible as
      // the confirmed selection, and resolve the span to a descriptor.
      suppressNextClick = true;
      var b = bounds();
      if (b) drawDragSpan(b, dragStartEpoch, dragCurrentEpoch, true);
      selectSpan(dragStartEpoch, dragCurrentEpoch);
    }
    dragStartX = null;
    dragStartEpoch = null;
    dragCurrentEpoch = null;
    dragMoved = false;
  }

  // --- Span -> descriptor --------------------------------------------------
  function csrfToken() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute("content") : "";
  }

  function rangeLabel(fromEpoch, toEpoch) {
    // Compact "YYYY-MM-DD HH:MM – YYYY-MM-DD HH:MM" (UTC), the same axis the
    // ribbon and date-jump use.
    return isoMinute(fromEpoch).replace("T", " ") + " – " + isoMinute(toEpoch).replace("T", " ");
  }

  // Build the in_range descriptor for the dragged span and POST it to
  // /frames/range/count, then hand the descriptor + estimate + label to the
  // selection spine, which owns the escalation banner and the bar's "≈N" label.
  function selectSpan(epochA, epochB) {
    var wrap = ribbonWrap();
    if (!wrap) return;
    var projectId = parseInt(wrap.getAttribute("data-project-id"), 10);
    if (isNaN(projectId)) return;
    var lo = Math.min(epochA, epochB);
    var hi = Math.max(epochA, epochB);
    var descriptor = {
      scope: "in_range",
      project_id: projectId,
      time_range: { from: isoFull(lo), to: isoFull(hi) },
      filters: { include_deleted: false },
      deselected_ids: [],
    };
    var label = rangeLabel(lo, hi);
    if (typeof htmx === "undefined" || !window.frameSelection) return;
    fetch("/frames/range/count", {
      method: "POST",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded",
        "X-CSRF-Token": csrfToken(),
        "HX-Request": "true",
      },
      body:
        "descriptor=" +
        encodeURIComponent(JSON.stringify(descriptor)) +
        "&csrf_token=" +
        encodeURIComponent(csrfToken()),
    })
      .then(function (r) {
        return r.ok ? r.json() : null;
      })
      .then(function (data) {
        var count = data && typeof data.count === "number" ? data.count : null;
        if (count === null) return;
        if (window.frameSelection.setDescriptor) {
          window.frameSelection.setDescriptor(descriptor, count, label);
        }
      })
      .catch(function () {
        /* A failed estimate leaves the highlight but shows no banner. */
      });
  }

  // Capture phase so we beat ribbon.js's bubble-phase click listener.
  document.addEventListener(
    "click",
    function (e) {
      if (!suppressNextClick) return;
      suppressNextClick = false;
      if (e.target && e.target.closest && e.target.closest(".frame-ribbon")) {
        e.stopPropagation();
        e.preventDefault();
      }
    },
    true
  );

  // --- Wiring --------------------------------------------------------------
  function init() {
    var wrap = ribbonWrap();
    if (!wrap) return;
    wrap.addEventListener("keydown", onSliderKey);
    document.addEventListener("pointerdown", onPointerDown);
    syncSliderAria();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  // The ribbon SVG (and its epoch bounds) loads late and reloads on project
  // switch; refresh the slider aria each time it settles.
  document.body.addEventListener("htmx:afterSettle", function (e) {
    var t = e.target;
    if (t && t.classList && t.classList.contains("frame-ribbon")) {
      keyEpoch = null; // re-baseline to the new project's newest edge
      clearZoom(); // drop any prior project's zoom strip
      syncSliderAria();
      // The overview's epoch bounds are now available; (re)load the zoom strip
      // for the current viewport in case its settle lost the race to the grid's.
      if (lastViewportCenter !== null) maybeUpdateZoom(lastViewportCenter);
    }
  });

  // When the selection spine clears a descriptor selection, drop the confirmed
  // span highlight so the ribbon no longer shows a stale committed range.
  document.addEventListener("selection:cleared", clearDragSpan);
})();
