/*
 * Screen-reader announcements for continuous-scroll batches.
 *
 * The frames grid and the events log each load older items on scroll: a
 * sentinel element carries hx-trigger="revealed" + hx-swap="outerHTML" and is
 * replaced by [older items + a fresh sentinel, or an end-cap]. Sighted users see
 * the new rows appear; without an announcement, assistive tech says nothing.
 *
 * On every content swap this re-scans the grid's current sentinel/end-cap and,
 * when it changes, writes one sentence into the page's polite live region. It
 * branches on data-batch-count so an end-cap that still carries rows is
 * announced as a load (not "beginning reached"); only a genuinely empty batch
 * (count 0) announces the start of the series.
 *
 * The first sight of the anchor (initial page render) is recorded silently, so
 * the live region starts empty and unrelated early swaps on the page (e.g. the
 * lazily-loaded status banner) do not trigger a spurious announcement.
 *
 * Markup contract (see frames_batch.html / events_batch.html):
 *   .frame-sentinel / .frame-end-cap  and  .log-sentinel / .log-end-cap carry
 *   data-batch-count, data-newest-id and (when count > 0) data-oldest-timestamp.
 */
(function () {
  "use strict";

  var GRIDS = {
    frame: {
      anchors: "#frame-grid .frame-sentinel, #frame-grid .frame-end-cap",
      statusId: "frame-load-status",
      noun: "frames",
      beginning: "Beginning of frames reached",
    },
    log: {
      anchors: "#events-tbody .log-sentinel, #events-tbody .log-end-cap",
      statusId: "events-load-status",
      noun: "events",
      beginning: "Beginning of the event log reached",
    },
    audit: {
      anchors: "#audit-tbody .log-sentinel, #audit-tbody .log-end-cap",
      statusId: "audit-load-status",
      noun: "records",
      beginning: "Beginning of the audit log reached",
    },
  };

  // Last-seen newest-id per grid; ``undefined`` until the first scan records the
  // initial batch silently, so the live region only speaks on later appends.
  var lastSeen = {};

  function scan(name, cfg) {
    var region = document.getElementById(cfg.statusId);
    if (!region) return; // this grid is not on the current page
    var anchor = document.querySelector(cfg.anchors);
    if (!anchor) return;

    var key = anchor.getAttribute("data-newest-id") || "";
    if (lastSeen[name] === undefined) {
      lastSeen[name] = key; // seed from the initial render; stay silent
      return;
    }
    if (lastSeen[name] === key) return; // no new batch since last scan
    lastSeen[name] = key;

    var count = parseInt(anchor.getAttribute("data-batch-count") || "0", 10);
    if (!count) {
      region.textContent = cfg.beginning;
      return;
    }
    var through = anchor.getAttribute("data-oldest-timestamp") || "";
    region.textContent = count + " " + cfg.noun + " loaded through " + through;
  }

  function scanAll() {
    for (var name in GRIDS) {
      if (Object.prototype.hasOwnProperty.call(GRIDS, name)) {
        scan(name, GRIDS[name]);
      }
    }
  }

  // Seed from the initial DOM, then react to every HTMX content swap. Listening
  // to both afterSwap and load is belt-and-suspenders: load is dispatched on
  // connected new content and reliably bubbles to body, covering the case where
  // afterSwap fires on a since-detached sentinel.
  scanAll();
  document.body.addEventListener("htmx:afterSwap", scanAll);
  document.body.addEventListener("htmx:load", scanAll);
})();
