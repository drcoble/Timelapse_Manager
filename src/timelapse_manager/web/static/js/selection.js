/*
 * Frame-grid selection spine.
 *
 * A single module-scoped explicit-id Set drives every selection behaviour on
 * the frames page. The set survives HTMX grid swaps (scroll-batch append,
 * same-project date-jump, show/hide deleted, new-frames refresh): after any
 * grid swap we re-apply the selected state to whichever tiles are still in the
 * DOM. A project switch is the one transition that clears the set (a different
 * project's ids are meaningless), guarded so a non-empty selection is not lost
 * silently.
 *
 * Markup contract (the tile template builds to exactly this):
 *   <div class="frame-tile" id="frame-tile-{id}" data-timestamp=...
 *        role="gridcell">                          (gets data-selected="true")
 *     <div class="frame-tile-thumb-wrap">
 *       <a ...drawer opener...></a>
 *       <label class="frame-tile-select">
 *         <input type="checkbox" class="frame-select-cb visually-hidden"
 *                data-frame-id="{id}" aria-label="Select frame {seq}">
 *         <span class="frame-select-indicator" aria-hidden="true"></span>
 *       </label>
 *     </div>
 *     ...
 *   </div>
 *
 * The thumbnail anchor opens the drawer; the label toggles selection. They are
 * structurally separate and must not trigger each other — a click inside the
 * select label has its propagation stopped so it never reaches the drawer
 * opener, and toggling never navigates.
 *
 * Selection-count changes are announced into the dedicated polite live region
 * #frame-action-status (NOT #frame-load-status, which scroll-announce.js
 * rescans and overwrites on every swap).
 */
(function () {
  "use strict";

  var ANNOUNCE_DEBOUNCE_MS = 250;

  // The explicit-id selection. Module-scoped so it persists across swaps.
  var selectedIds = new Set();
  // The last checkbox the user toggled, for Shift-range. Reset when its tile
  // leaves the DOM (a stale anchor cannot define a contiguous run).
  var lastToggledId = null;

  // --- descriptor mode -----------------------------------------------------
  // The selection spine is a 3-state machine: empty -> explicit id-set ->
  // descriptor (a "select all in range / project"), mutually exclusive. In
  // descriptor mode the selection is NOT an enumerated id-set: it is a small
  // descriptor the server resolves, plus the running estimate and the ids the
  // user has since deselected (subtracted server-side, never re-enumerated).
  var descriptor = null; // {scope, project_id, time_range, filters, deselected_ids}
  var descriptorCount = 0; // the running ≈N estimate, decremented on deselect
  var descriptorLabel = ""; // the human range/scope label shown after "≈N · "

  function inDescriptorMode() {
    return descriptor !== null;
  }

  function grid() {
    return document.getElementById("frame-grid");
  }

  function actionBar() {
    return document.getElementById("frames-action-bar");
  }

  function tileForId(id) {
    return document.getElementById("frame-tile-" + id);
  }

  function checkboxes() {
    var g = grid();
    return g ? g.querySelectorAll(".frame-select-cb") : [];
  }

  function idOf(cb) {
    return cb.getAttribute("data-frame-id");
  }

  // --- selection state -----------------------------------------------------

  function applyTileState(id, on) {
    var tile = tileForId(id);
    if (tile) {
      if (on) tile.setAttribute("data-selected", "true");
      else tile.removeAttribute("data-selected");
    }
    var cb = document.querySelector(
      '.frame-select-cb[data-frame-id="' + id + '"]'
    );
    if (cb) cb.checked = on;
  }

  function setSelected(id, on) {
    if (on) selectedIds.add(id);
    else selectedIds.delete(id);
    applyTileState(id, on);
  }

  // The number of selected tiles currently rendered in the grid. Differs from
  // selectedIds.size after a batch append/jump where some selected frames have
  // scrolled out of (or never entered) the DOM.
  function visibleSelectedCount() {
    var n = 0;
    selectedIds.forEach(function (id) {
      if (tileForId(id)) n += 1;
    });
    return n;
  }

  function countText() {
    if (inDescriptorMode()) {
      // Approximate, range-scoped: the server resolves the descriptor; the count
      // is an estimate decremented locally as tiles are deselected.
      var n = descriptorCount;
      var deselected = descriptor.deselected_ids.length;
      var base = "≈" + n + (descriptorLabel ? " · " + descriptorLabel : "");
      if (deselected > 0) {
        base += " (" + deselected + " deselected)";
      }
      return base;
    }
    var total = selectedIds.size;
    var noun = total === 1 ? "frame" : "frames";
    var visible = visibleSelectedCount();
    if (visible < total) {
      return total + " selected (" + visible + " visible)";
    }
    return total + " " + noun + " selected";
  }

  // The selection-bar markup, captured before any result swap replaces it, so a
  // fresh selection can restore the action bar from its result state.
  var selectionBarHTML = null;

  function captureSelectionBar() {
    var bar = actionBar();
    if (bar && !bar.classList.contains("frames-action-bar--result")) {
      selectionBarHTML = bar.outerHTML;
    }
  }

  // Restore the selection-bar markup if the bar is currently in result state.
  // Returns the (possibly replaced) bar element.
  function ensureSelectionBar() {
    var bar = actionBar();
    if (bar && bar.classList.contains("frames-action-bar--result") && selectionBarHTML) {
      bar.outerHTML = selectionBarHTML;
      bar = actionBar();
    }
    return bar;
  }

  function updateBar() {
    var has = selectedIds.size > 0 || inDescriptorMode();
    var bar = actionBar();
    if (!bar) return;
    // A new selection out of the result state restores the selection bar so its
    // action buttons (not the result's Undo/Retry) are what the user sees.
    if (has) bar = ensureSelectionBar();
    if (!bar) return;
    // Never hide a result bar on a clear-after-success — only the selection bar
    // collapses to empty; the result bar stays until dismissed or superseded.
    if (!bar.classList.contains("frames-action-bar--result")) {
      bar.hidden = !has;
    }
    var countEl = bar.querySelector(".selection-bar-count");
    if (countEl && !bar.classList.contains("frames-action-bar--result")) {
      countEl.textContent = has ? countText() : "";
    }
  }

  // --- announcements (debounced; dedicated live region) --------------------

  var announceTimer = null;
  function announce() {
    if (announceTimer) window.clearTimeout(announceTimer);
    announceTimer = window.setTimeout(function () {
      var region = document.getElementById("frame-action-status");
      if (!region) return;
      region.textContent =
        selectedIds.size > 0 || inDescriptorMode() ? countText() : "Selection cleared";
    }, ANNOUNCE_DEBOUNCE_MS);
  }

  function refresh() {
    updateBar();
    announce();
  }

  // Clear the Set and every tile's selected state, but leave the action bar
  // untouched. Used after a successful bulk op, where the result bar has already
  // replaced the selection bar and must not be hidden by a bar refresh.
  function clearSelectionState(keepIds) {
    var keep = keepIds || [];
    var ids = [];
    selectedIds.forEach(function (id) {
      ids.push(id);
    });
    selectedIds.clear();
    lastToggledId = null;
    for (var i = 0; i < ids.length; i++) {
      var id = ids[i];
      if (keep.indexOf(id) === -1) applyTileState(id, false);
      else selectedIds.add(id);
    }
    // Leaving a descriptor selection: drop every tile that the descriptor mode
    // had visually marked selected, tear down the descriptor, hide the banner,
    // and tell the scrubber to drop its confirmed span highlight.
    if (inDescriptorMode()) {
      var cbs = checkboxes();
      for (var k = 0; k < cbs.length; k++) {
        applyTileState(idOf(cbs[k]), false);
      }
      descriptor = null;
      descriptorCount = 0;
      descriptorLabel = "";
      hideEscalationBanner();
      document.dispatchEvent(new CustomEvent("selection:cleared"));
    }
  }

  function clearSelection() {
    clearSelectionState();
    refresh();
  }

  // --- Shift-range ---------------------------------------------------------
  // Toggle every checkbox between the last-toggled tile and the shift-clicked
  // one (inclusive) to the clicked tile's new state, in current DOM order.
  function applyRange(fromId, toId, on) {
    var cbs = checkboxes();
    var fromIdx = -1;
    var toIdx = -1;
    for (var i = 0; i < cbs.length; i++) {
      var id = idOf(cbs[i]);
      if (id === fromId) fromIdx = i;
      if (id === toId) toIdx = i;
    }
    if (fromIdx === -1 || toIdx === -1) return false;
    var lo = Math.min(fromIdx, toIdx);
    var hi = Math.max(fromIdx, toIdx);
    for (var j = lo; j <= hi; j++) {
      setSelected(idOf(cbs[j]), on);
    }
    return true;
  }

  // --- event delegation ----------------------------------------------------
  // A click inside the select label must never reach the drawer opener anchor;
  // stop it here. Range logic reads shiftKey (only present on click). The Set
  // is synced from the checkbox's resulting state on the change event.
  document.body.addEventListener(
    "click",
    function (e) {
      if (!e.target || typeof e.target.closest !== "function") return;
      var label = e.target.closest(".frame-tile-select");
      if (!label) return;
      // Selection interaction — never let it bubble to the drawer opener.
      e.stopPropagation();
      var cb = label.querySelector(".frame-select-cb");
      if (!cb) return;
      // The label's native click is what toggles the checkbox; we only need to
      // intercept Shift-range here. The new state is the checkbox's state AFTER
      // this click, i.e. the opposite of its current state.
      if (e.shiftKey && lastToggledId !== null && idOf(cb) !== lastToggledId) {
        var willBe = !cb.checked;
        if (applyRange(lastToggledId, idOf(cb), willBe)) {
          // The browser will still toggle cb via the label; applyRange already
          // set it to willBe, so suppress the synthetic toggle to avoid a flip.
          e.preventDefault();
          lastToggledId = idOf(cb);
          refresh();
        }
      }
    },
    true // capture: stop the click before the drawer's bubble-phase handler
  );

  // Single-toggle truth source: the checkbox's resulting checked state.
  document.body.addEventListener("change", function (e) {
    var cb =
      e.target && e.target.classList && e.target.classList.contains("frame-select-cb")
        ? e.target
        : null;
    if (!cb) return;
    var id = idOf(cb);
    if (inDescriptorMode()) {
      // Every in-range tile is conceptually selected, so toggling a tile off
      // appends it to deselected_ids (subtracted server-side; never
      // re-enumerated) and decrements the estimate; toggling it back removes it.
      toggleDescriptorDeselect(id, cb.checked);
      lastToggledId = id;
      refresh();
      return;
    }
    setSelected(id, cb.checked);
    lastToggledId = id;
    refresh();
  });

  // In descriptor mode, reflect a tile's checked state into deselected_ids.
  // `checked` true means "back in the selection" (remove from deselected);
  // false means "deselected" (add, decrement the estimate). Idempotent.
  function toggleDescriptorDeselect(id, checked) {
    if (!inDescriptorMode()) return;
    var list = descriptor.deselected_ids;
    var asInt = parseInt(id, 10);
    var idx = list.indexOf(asInt);
    if (checked) {
      if (idx !== -1) {
        list.splice(idx, 1);
        descriptorCount += 1;
      }
    } else if (idx === -1) {
      list.push(asInt);
      descriptorCount = Math.max(0, descriptorCount - 1);
    }
    applyTileState(id, checked);
  }

  // Clear / Dismiss button on the action bar. On the selection bar it clears the
  // selection (collapsing the bar); on the result bar it dismisses the result,
  // dropping any failed ids still held and hiding the bar.
  document.body.addEventListener("click", function (e) {
    if (!e.target || typeof e.target.closest !== "function") return;
    if (!e.target.closest("[data-selection-clear]")) return;
    e.preventDefault();
    var bar = actionBar();
    if (bar && bar.classList.contains("frames-action-bar--result")) {
      clearSelectionState();
      bar.hidden = true;
      announce();
    } else {
      clearSelection();
    }
  });

  // --- re-highlight after grid swaps ---------------------------------------
  // After a batch append / date-jump / refresh, re-apply data-selected + check
  // to every selected id still present. Newly-appended tiles not in the set
  // render unselected (the template never emits data-selected on its own).
  function rehighlight() {
    if (inDescriptorMode()) {
      // In descriptor mode every tile in the grid is part of the selection
      // except those the user has deselected; re-apply that to whatever tiles
      // are now present (scroll append / jump may have brought in new ones).
      var deselected = descriptor.deselected_ids;
      var cbs = checkboxes();
      for (var i = 0; i < cbs.length; i++) {
        var cid = idOf(cbs[i]);
        var off = deselected.indexOf(parseInt(cid, 10)) !== -1;
        applyTileState(cid, !off);
      }
    } else {
      selectedIds.forEach(function (id) {
        applyTileState(id, true);
      });
    }
    // The last-range anchor may have scrolled out / been replaced; drop it so
    // a later Shift-click cannot span across a gone tile.
    if (lastToggledId !== null && !tileForId(lastToggledId)) {
      lastToggledId = null;
    }
    updateBar();
  }

  function onGridSwap(e) {
    var t = e.target;
    if (!t) return;
    if (t.id === "frame-grid" || (t.closest && t.closest("#frame-grid"))) {
      rehighlight();
    }
  }
  document.body.addEventListener("htmx:afterSettle", onGridSwap);
  document.body.addEventListener("htmx:afterSwap", onGridSwap);

  // --- drawer selection bridge ---------------------------------------------
  // When the single-frame drawer opens on a frame that belongs to a multi-
  // selection, surface that context so the operator can pivot to the bulk
  // action bar rather than editing one frame at a time. The drawer body is
  // HTMX-rendered with no selection context server-side, so the bridge is
  // injected here from the live selection. It is rebuilt on every drawer swap
  // because prev/next navigation re-swaps the same body in place. "Act on all"
  // simply closes the drawer, revealing the grid with the bulk action bar;
  // "Edit just this one" is the drawer itself, so it needs no control.
  function drawerBody() {
    var d = document.getElementById("drawer-main");
    return d ? d.querySelector(".drawer-body") : null;
  }

  function updateDrawerBridge() {
    var body = drawerBody();
    if (!body) return;
    var old = body.querySelector(".drawer-selection-bridge");
    if (old) old.remove();
    var root = body.querySelector("[data-frame-id]");
    if (!root) return;
    var id = String(root.getAttribute("data-frame-id"));
    var n = selectedIds.size;
    if (n < 2 || !selectedIds.has(id)) return;

    var bridge = document.createElement("div");
    bridge.className = "drawer-selection-bridge";
    bridge.setAttribute("role", "note");

    var label = document.createElement("span");
    label.className = "drawer-selection-bridge-label";
    label.textContent = "Part of a selection of " + n + " frames.";

    var act = document.createElement("button");
    act.type = "button";
    act.className = "btn btn-sm btn-secondary drawer-selection-bridge-act";
    act.setAttribute("data-drawer-close", "");
    act.textContent = "Act on all " + n;

    bridge.appendChild(label);
    bridge.appendChild(act);
    body.insertBefore(bridge, body.firstChild);
  }

  function onDrawerSwap(e) {
    var t = e.target;
    if (t && t.closest && t.closest("#drawer-main")) updateDrawerBridge();
  }
  document.body.addEventListener("htmx:afterSettle", onDrawerSwap);

  // --- project-switch guard ------------------------------------------------
  // The project <select> carries an inline onchange="...requestSubmit()". A
  // listener bound on the <select> itself cannot beat that inline handler:
  // at AT_TARGET, listeners fire in registration order regardless of the
  // capture flag, and the inline attribute was registered at parse time. So we
  // bind on `document` in the CAPTURE phase, where the capture run genuinely
  // precedes the target — and stopPropagation there keeps the event from ever
  // reaching the inline onchange.
  //
  // We track the previously-selected project so a cancel can revert the
  // <select> value (the change event fires after the value has already moved).
  var prevProjectValue = null;

  function rememberProject() {
    var sel = document.getElementById("frame-project-select");
    if (sel) prevProjectValue = sel.value;
  }
  rememberProject();
  document.body.addEventListener("htmx:afterSettle", rememberProject);

  document.addEventListener(
    "change",
    function (e) {
      var sel = e.target;
      if (!sel || sel.id !== "frame-project-select") return;
      // A descriptor selection is range-scoped to the current project, so it is
      // just as meaningful to lose as an explicit id-set — guard both. (The
      // selection clears naturally on the full-page navigation either way; this
      // is the "you'll lose your selection" confirmation.)
      if (selectedIds.size === 0 && !inDescriptorMode()) {
        // Nothing to lose — let the inline onchange submit normally.
        prevProjectValue = sel.value;
        return;
      }
      var n = selectedIds.size;
      var ok = window.confirm(
        inDescriptorMode()
          ? "Switching projects clears the current range selection. Continue?"
          : "Switching projects clears " +
              n +
              (n === 1 ? " selected frame." : " selected frames.") +
              " Continue?"
      );
      if (!ok) {
        // Suppress the inline submit and restore the prior project.
        e.stopPropagation();
        e.preventDefault();
        if (prevProjectValue !== null) sel.value = prevProjectValue;
        return;
      }
      // Confirmed: drop the now-irrelevant selection and let the navigation
      // proceed (do NOT stopPropagation — the inline onchange must still fire).
      clearSelection();
      prevProjectValue = sel.value;
    },
    true // capture phase — beats the inline onchange on the <select> itself
  );

  // --- bulk actions --------------------------------------------------------
  // The action-bar buttons serialise the current selection into ONE bulk POST.
  // The request rides htmx.ajax (never raw fetch) so the per-session CSRF token
  // on <body hx-headers> is carried; we also pass it explicitly from the meta
  // tag as a belt-and-braces guarantee. The result partial swaps the action bar
  // (outerHTML) and re-renders affected tiles out-of-band.
  var BULK_URL = "/frames/bulk";

  function csrfToken() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute("content") : "";
  }

  function announceResult(text, assertive) {
    var region = document.getElementById("frame-action-status");
    if (!region) return;
    region.setAttribute("aria-live", assertive ? "assertive" : "polite");
    region.textContent = text;
  }

  // Fire one bulk operation over an explicit id list; failed ids are re-read
  // from the result partial and kept selected for Retry.
  function postBulk(operation, ids) {
    if (!ids.length) return;
    sendBulk({ operation: operation, frame_ids: ids.join(",") });
  }

  // Fire one bulk operation over the current descriptor (select-all-in-range /
  // -in-project). The server resolves it to the id-set and subtracts the
  // deselected ids, so the descriptor travels as one small JSON value rather than
  // thousands of ids.
  function postBulkDescriptor(operation) {
    if (!inDescriptorMode()) return;
    sendBulk({ operation: operation, descriptor: JSON.stringify(descriptor) });
  }

  function sendBulk(values) {
    var bar = actionBar();
    htmx
      .ajax("POST", BULK_URL, {
        source: bar || document.body,
        target: "#frames-action-bar",
        swap: "outerHTML",
        headers: { "X-CSRF-Token": csrfToken() },
        values: values,
      })
      .catch(function () {
        announceResult("Bulk action failed to send.", true);
      });
  }

  // After the bulk result partial swaps into #frames-action-bar, reconcile the
  // client selection with what the server reported: drop the succeeded ids
  // (the acted set is consumed) and keep the failed ids selected for Retry.
  function onBulkResultSwapped(resultBar) {
    var reload = resultBar.getAttribute("data-bulk-reload-window") === "1";
    var countEl = resultBar.querySelector(".selection-bar-count");
    var summaryText = countEl ? countEl.textContent.trim() : "Done";

    // Collect the ids the result still wants kept selected (Retry's failed set).
    var keep = [];
    var retryBtn = resultBar.querySelector("[data-bulk-retry]");
    if (retryBtn) {
      var raw = retryBtn.getAttribute("data-frame-ids") || "";
      raw.split(",").forEach(function (s) {
        s = s.trim();
        if (s) keep.push(s);
      });
    }
    // Drop the acted set; keep failed ids selected. Do NOT touch the bar (the
    // result partial is already in place and must stay visible).
    clearSelectionState(keep);

    var hadFailures = keep.length > 0;
    announceResult(summaryText, hadFailures);

    if (reload) reloadGridWindow();
  }

  // Reload the current grid window for a large operation, so the grid reflects
  // the change without a wall of out-of-band tiles. Requests the batch route,
  // which returns the tiles+sentinel fragment (not the full page) for both the
  // single-project and All-Projects grids; the re-highlight pass then re-applies
  // any kept selection. The page's query string carries project_id (or none for
  // All-Projects) and show_deleted, so the reloaded window matches the view.
  function reloadGridWindow() {
    var g = grid();
    if (!g || typeof htmx === "undefined") return;
    htmx.ajax("GET", "/frames/batch" + window.location.search, {
      target: "#frame-grid",
      swap: "innerHTML",
    });
  }

  // Delete is destructive: the first click arms an in-bar confirm, the second
  // (the confirm button) fires. Any other interaction disarms it.
  var deleteArmed = false;

  function disarmDelete() {
    if (!deleteArmed) return;
    deleteArmed = false;
    var btn = document.querySelector('[data-bulk-action="delete"]');
    if (btn) {
      btn.textContent = "Delete";
      btn.classList.remove("is-armed");
    }
  }

  function armDelete() {
    deleteArmed = true;
    var btn = document.querySelector('[data-bulk-action="delete"]');
    if (btn) {
      btn.textContent = "Confirm delete";
      btn.classList.add("is-armed");
    }
  }

  document.body.addEventListener("click", function (e) {
    if (!e.target || typeof e.target.closest !== "function") return;

    var actionBtn = e.target.closest("[data-bulk-action]");
    if (actionBtn) {
      e.preventDefault();
      var op = actionBtn.getAttribute("data-bulk-action");
      var ids = window.frameSelection.ids();
      // In descriptor mode the selection is the resolved range, not an id-set.
      if (!inDescriptorMode() && !ids.length) return;
      if (op === "delete") {
        if (!deleteArmed) {
          armDelete();
          return;
        }
        disarmDelete();
      }
      if (inDescriptorMode()) postBulkDescriptor(op);
      else postBulk(op, ids);
      return;
    }

    var undoBtn = e.target.closest("[data-bulk-undo]");
    if (undoBtn) {
      e.preventDefault();
      postBulk(
        undoBtn.getAttribute("data-bulk-operation"),
        idsAttr(undoBtn)
      );
      return;
    }

    var retryBtn = e.target.closest("[data-bulk-retry]");
    if (retryBtn) {
      e.preventDefault();
      postBulk(
        retryBtn.getAttribute("data-bulk-operation"),
        idsAttr(retryBtn)
      );
      return;
    }

    // Any unrelated click disarms a pending delete confirm.
    if (deleteArmed && !e.target.closest('[data-bulk-action="delete"]')) {
      disarmDelete();
    }
  });

  function idsAttr(el) {
    var raw = el.getAttribute("data-frame-ids") || "";
    var out = [];
    raw.split(",").forEach(function (s) {
      s = s.trim();
      if (s) out.push(s);
    });
    return out;
  }

  // Process the bulk result once the action bar has been swapped to it. An
  // outerHTML swap removes the old element, so htmx:afterSwap may fire on the
  // parent rather than the new bar — do not rely on e.target being the bar.
  // Instead look the bar up by id and process it once (guarded by a marker the
  // result markup does not carry, so a fresh result is always processed and an
  // already-processed one is skipped).
  function maybeProcessBulkResult() {
    var bar = actionBar();
    if (!bar) return;
    if (!bar.classList.contains("frames-action-bar--result")) return;
    if (bar.getAttribute("data-bulk-processed") === "1") return;
    bar.setAttribute("data-bulk-processed", "1");
    onBulkResultSwapped(bar);
  }
  document.body.addEventListener("htmx:afterSwap", maybeProcessBulkResult);
  document.body.addEventListener("htmx:afterSettle", maybeProcessBulkResult);

  // --- bulk timestamp offset -----------------------------------------------
  // The Offset button reveals an inline panel (direction + h/m/s). Apply shifts
  // every selected frame's capture time by the signed seconds. A range
  // selection is materialised to ids FIRST (offsetting a selection defined by
  // the times being changed is self-referential). The route returns JSON; on
  // success the window reloads (a shift reorders the grid) and the bar shows an
  // inverse-offset Undo.
  function offsetPanel() {
    return document.getElementById("frames-offset-panel");
  }

  function offsetSeconds() {
    var p = offsetPanel();
    if (!p) return 0;
    function val(sel) {
      var el = p.querySelector(sel);
      return el ? parseInt(el.value, 10) || 0 : 0;
    }
    var dir = p.querySelector('input[name="offset-dir"]:checked');
    var sign = dir && dir.value === "-" ? -1 : 1;
    return sign * (val("[data-offset-h]") * 3600 + val("[data-offset-m]") * 60 + val("[data-offset-s]"));
  }

  function humanDur(secs) {
    var sign = secs < 0 ? "−" : "+";
    var a = Math.abs(secs);
    var parts = [];
    var h = Math.floor(a / 3600);
    var m = Math.floor((a % 3600) / 60);
    var s = a % 60;
    if (h) parts.push(h + "h");
    if (m) parts.push(m + "m");
    if (s || !parts.length) parts.push(s + "s");
    return sign + parts.join(" ");
  }

  function fmtClock(epoch) {
    var d = new Date(epoch * 1000);
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  function earliestVisibleSelectedEpoch() {
    var best = null;
    document
      .querySelectorAll('#frame-grid .frame-tile[data-selected="true"][data-timestamp]')
      .forEach(function (t) {
        var e = parseInt(t.getAttribute("data-timestamp"), 10);
        if (!isNaN(e) && (best === null || e < best)) best = e;
      });
    return best;
  }

  function updateOffsetPreview() {
    var p = offsetPanel();
    if (!p) return;
    var prev = p.querySelector("[data-offset-preview]");
    if (!prev) return;
    var secs = offsetSeconds();
    if (!secs) {
      prev.textContent = "Choose a non-zero shift.";
      return;
    }
    var label = "Shift by " + humanDur(secs);
    var e = earliestVisibleSelectedEpoch();
    if (e !== null) label += " — e.g. " + fmtClock(e) + " → " + fmtClock(e + secs);
    prev.textContent = label;
  }

  function toggleOffsetPanel(show) {
    var p = offsetPanel();
    if (!p) return;
    var open = show === undefined ? p.hasAttribute("hidden") : show;
    if (open) {
      p.removeAttribute("hidden");
      updateOffsetPreview();
    } else {
      p.setAttribute("hidden", "");
    }
    var b = document.querySelector("[data-offset-toggle]");
    if (b) b.setAttribute("aria-expanded", open ? "true" : "false");
  }

  function urlencode(obj) {
    return Object.keys(obj)
      .map(function (k) {
        return k + "=" + encodeURIComponent(obj[k]);
      })
      .join("&");
  }

  function postForm(url, values) {
    return fetch(url, {
      method: "POST",
      headers: {
        "X-CSRF-Token": csrfToken(),
        "Content-Type": "application/x-www-form-urlencoded",
      },
      body: urlencode(values),
    });
  }

  function postOffset(ids, secs) {
    return postForm("/frames/offset", { frame_ids: ids, seconds: secs }).then(function (r) {
      return r.json().then(function (j) {
        return { ok: r.ok, j: j };
      });
    });
  }

  function showOffsetResult(result) {
    captureSelectionBar();
    var bar = actionBar();
    if (!bar || !result.undo) return;
    bar.classList.add("frames-action-bar--result");
    bar.hidden = false;
    var msg =
      result.shifted +
      " frame" +
      (result.shifted === 1 ? "" : "s") +
      " shifted " +
      humanDur(result.seconds) +
      (result.skipped_null ? " (" + result.skipped_null + " had no time)" : "");
    bar.innerHTML =
      '<span class="selection-bar-count">' +
      msg +
      "</span>" +
      '<button type="button" class="btn btn-sm btn-secondary" data-offset-undo ' +
      'data-frame-ids="' +
      result.undo.frame_ids.join(",") +
      '" data-seconds="' +
      result.undo.seconds +
      '">Undo</button>' +
      '<button type="button" class="btn btn-sm btn-ghost" data-offset-dismiss ' +
      'aria-label="Dismiss">×</button>';
  }

  function dismissOffsetResult() {
    var bar = actionBar();
    if (bar && bar.classList.contains("frames-action-bar--result")) {
      bar.classList.remove("frames-action-bar--result");
      bar.hidden = true;
    }
  }

  function applyOffset() {
    var secs = offsetSeconds();
    if (!secs) {
      announceResult("Choose a non-zero shift.", true);
      return;
    }
    var idsPromise;
    if (inDescriptorMode()) {
      idsPromise = postForm("/frames/range/materialize", {
        descriptor: JSON.stringify(descriptor),
      })
        .then(function (r) {
          return r.json();
        })
        .then(function (j) {
          return (j.frame_ids || []).join(",");
        });
    } else {
      idsPromise = Promise.resolve(window.frameSelection.ids().join(","));
    }
    idsPromise
      .then(function (ids) {
        if (!ids) {
          announceResult("No frames to shift.", true);
          return null;
        }
        return postOffset(ids, secs);
      })
      .then(function (res) {
        if (!res) return;
        if (!res.ok) {
          announceResult(res.j.error || "Offset failed.", true);
          return;
        }
        toggleOffsetPanel(false);
        clearSelection();
        showOffsetResult(res.j);
        reloadGridWindow();
        announceResult(
          res.j.shifted + " frames shifted " + humanDur(res.j.seconds),
          false
        );
      })
      .catch(function () {
        announceResult("Offset failed to send.", true);
      });
  }

  document.body.addEventListener("click", function (e) {
    if (!e.target || typeof e.target.closest !== "function") return;
    if (e.target.closest("[data-offset-toggle]")) {
      e.preventDefault();
      toggleOffsetPanel();
      return;
    }
    if (e.target.closest("[data-offset-cancel]")) {
      e.preventDefault();
      toggleOffsetPanel(false);
      return;
    }
    if (e.target.closest("[data-offset-apply]")) {
      e.preventDefault();
      applyOffset();
      return;
    }
    var undo = e.target.closest("[data-offset-undo]");
    if (undo) {
      e.preventDefault();
      postOffset(undo.getAttribute("data-frame-ids"), undo.getAttribute("data-seconds")).then(
        function (res) {
          announceResult(res.ok ? "Offset undone." : res.j.error || "Undo failed.", !res.ok);
          dismissOffsetResult();
          reloadGridWindow();
        }
      );
      return;
    }
    if (e.target.closest("[data-offset-dismiss]")) {
      e.preventDefault();
      dismissOffsetResult();
      return;
    }
  });

  document.body.addEventListener("input", function (e) {
    if (e.target && e.target.closest && e.target.closest("#frames-offset-panel")) {
      updateOffsetPreview();
    }
  });

  // --- bulk export (async zip) ---------------------------------------------
  // Export enqueues a background job that zips the selected frames. The action
  // bar shows progress (polled every 2s) and, when ready, a download link. The
  // job is JSON-driven (the routes return JSON, not partials), so the status UI
  // is built here in the bar's result state.
  var exportPoll = null;

  function stopExportPoll() {
    if (exportPoll) {
      window.clearInterval(exportPoll);
      exportPoll = null;
    }
  }

  function exportStatusHtml(state, jobId, progress) {
    var dismiss =
      '<button type="button" class="btn btn-sm btn-ghost" data-offset-dismiss ' +
      'aria-label="Dismiss">×</button>';
    if (state === "done") {
      return (
        '<span class="selection-bar-count">Export ready</span>' +
        '<a class="btn btn-sm btn-secondary" download ' +
        'href="/frames/export/' +
        jobId +
        '/download">Download zip</a>' +
        dismiss
      );
    }
    if (state === "failed") {
      return '<span class="selection-bar-count">Export failed</span>' + dismiss;
    }
    return (
      '<span class="selection-bar-count">Preparing export…</span>' +
      '<span class="progress-bar export-progress"><span class="progress-fill" ' +
      'style="width:' +
      (progress || 0) +
      '%"></span></span>' +
      dismiss
    );
  }

  function showExportStatus(html) {
    captureSelectionBar();
    var bar = actionBar();
    if (!bar) return;
    bar.classList.add("frames-action-bar--result");
    bar.hidden = false;
    bar.innerHTML = html;
  }

  function pollExport(jobId) {
    fetch("/frames/export/" + jobId)
      .then(function (r) {
        return r.json();
      })
      .then(function (j) {
        if (j.status === "done") {
          stopExportPoll();
          showExportStatus(exportStatusHtml("done", jobId));
          announceResult("Export ready to download.", false);
        } else if (j.status === "failed") {
          stopExportPoll();
          showExportStatus(exportStatusHtml("failed", jobId));
          announceResult("Export failed.", true);
        } else {
          showExportStatus(exportStatusHtml(j.status, jobId, j.progress));
        }
      })
      .catch(function () {});
  }

  function startExport() {
    var values = inDescriptorMode()
      ? { descriptor: JSON.stringify(descriptor) }
      : { frame_ids: window.frameSelection.ids().join(",") };
    if (!inDescriptorMode() && !values.frame_ids) return;
    postForm("/frames/export", values)
      .then(function (r) {
        return r.json().then(function (j) {
          return { ok: r.ok, j: j };
        });
      })
      .then(function (res) {
        if (!res.ok) {
          announceResult(res.j.error || "Export failed to start.", true);
          return;
        }
        var jobId = res.j.job_id;
        clearSelection();
        showExportStatus(exportStatusHtml("pending", jobId, 0));
        announceResult("Preparing export…", false);
        stopExportPoll();
        exportPoll = window.setInterval(function () {
          pollExport(jobId);
        }, 2000);
      })
      .catch(function () {
        announceResult("Export failed to send.", true);
      });
  }

  document.body.addEventListener("click", function (e) {
    if (!e.target || typeof e.target.closest !== "function") return;
    if (e.target.closest("[data-export-action]")) {
      e.preventDefault();
      startExport();
    }
  });

  // --- escalation banner ---------------------------------------------------
  // The Gmail-style banner shown after a drag-select: "≈N frames in <range>" with
  // [Select all in range] [Select all in project] [Dismiss]. The placeholder is
  // present in the markup (hidden); we only reveal and populate it.
  function escalationBanner() {
    return document.getElementById("frames-escalation-banner");
  }

  function showEscalationBanner(count, label) {
    var banner = escalationBanner();
    if (!banner) return;
    var countEl = banner.querySelector("[data-escalation-count]");
    if (countEl) {
      var noun = count === 1 ? "frame" : "frames";
      countEl.textContent =
        "≈" + count + " " + noun + (label ? " in " + label : "");
    }
    banner.hidden = false;
  }

  function hideEscalationBanner() {
    var banner = escalationBanner();
    if (banner) banner.hidden = true;
  }

  // Enter (or upgrade) descriptor mode. Called by the scrubber after a drag-span
  // is counted, and by the banner's scope buttons after they re-count. Clears any
  // explicit id-selection first (the two states are mutually exclusive).
  function setDescriptor(desc, count, label) {
    if (!desc) return;
    if (selectedIds.size > 0) clearSelectionState();
    // Normalise so deselected_ids is always a fresh array we own.
    descriptor = {
      scope: desc.scope,
      project_id: desc.project_id,
      time_range: desc.time_range || null,
      filters: desc.filters || { include_deleted: false },
      deselected_ids: (desc.deselected_ids || []).slice(),
    };
    descriptorCount = typeof count === "number" ? count : 0;
    descriptorLabel = label || "";
    showEscalationBanner(descriptorCount, descriptorLabel);
    rehighlight();
    refresh();
  }

  // The banner's "Select all in project" upgrades the scope to the whole campaign
  // (null-timestamp frames included). It must re-count: the project total is not
  // the range total. POSTs the upgraded descriptor to /frames/range/count, then
  // re-enters descriptor mode with the fresh estimate.
  function escalateScope(scope, label) {
    if (!inDescriptorMode()) return;
    var upgraded = {
      scope: scope,
      project_id: descriptor.project_id,
      filters: descriptor.filters,
      deselected_ids: [],
    };
    if (scope === "in_range") upgraded.time_range = descriptor.time_range;
    var body =
      "descriptor=" +
      encodeURIComponent(JSON.stringify(upgraded)) +
      "&csrf_token=" +
      encodeURIComponent(csrfToken());
    fetch("/frames/range/count", {
      method: "POST",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded",
        "X-CSRF-Token": csrfToken(),
        "HX-Request": "true",
      },
      body: body,
    })
      .then(function (r) {
        return r.ok ? r.json() : null;
      })
      .then(function (data) {
        if (!data || typeof data.count !== "number") return;
        setDescriptor(upgraded, data.count, label);
      })
      .catch(function () {
        /* leave the current descriptor in place on a failed re-count */
      });
  }

  document.body.addEventListener("click", function (e) {
    if (!e.target || typeof e.target.closest !== "function") return;
    var scopeBtn = e.target.closest("[data-escalation-scope]");
    if (scopeBtn) {
      e.preventDefault();
      var scope = scopeBtn.getAttribute("data-escalation-scope");
      var label = scopeBtn.getAttribute("data-escalation-label") || "";
      if (scope === "in_range") {
        // Already the dragged range — just confirm the current descriptor.
        showEscalationBanner(descriptorCount, descriptorLabel);
        refresh();
      } else {
        escalateScope(scope, label);
      }
      return;
    }
    if (e.target.closest("[data-escalation-dismiss]")) {
      e.preventDefault();
      clearSelection();
    }
  });

  // --- public accessor for later phases ------------------------------------
  // Phase 3 serializes the explicit-id set into a bulk request; Phase 5 adds the
  // descriptor mode (setDescriptor). Keep minimal.
  window.frameSelection = {
    ids: function () {
      var out = [];
      selectedIds.forEach(function (id) {
        out.push(id);
      });
      return out;
    },
    size: function () {
      return selectedIds.size;
    },
    has: function (id) {
      return selectedIds.has(String(id));
    },
    descriptor: function () {
      return descriptor;
    },
    setDescriptor: setDescriptor,
    clear: clearSelection,
  };

  // --- init (idempotent across HTMX swaps) ---------------------------------
  // Delegated listeners are bound once at IIFE scope above, so init only needs
  // to reconcile the bar with whatever is already selected (e.g. after the
  // script loads late on an HTMX-swapped page).
  function init() {
    captureSelectionBar();
    rehighlight();
    refresh();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
