/*
 * Behaviour of the project and schedule forms (#70 F-S3, WP-70-M1).
 *
 * Replaces the inline <script> blocks and on*= handlers that lived in
 * capture_schedule_fields, _exact_time_fieldset, _event_triggers_fieldset,
 * new_project_form, project_settings_form and milestone_form. Controls carry
 * `data-action` (see actions.js); the work happens in delegated handlers, so
 * a form swapped into the drawer or a tab behaves like one on a full page.
 * The one thing the inline scripts did on load, applying the capture mode to
 * the interval fields, runs from htmx.onLoad on each loaded fragment.
 *
 * Actions:
 *   sched-reveal   (change) schedule preset radio: show the sun / custom panel.
 *   capture-mode   (change) capture mode radio: interval vs. solar fields.
 *   exact-add      (click)  add an exact-time row from the template.
 *   exact-remove   (click)  remove an exact-time row.
 *   exact-kind     (change) anchor kind select: show or hide the clock time.
 *   trigger-add    (click)  add an event-trigger row from the template.
 *   trigger-remove (click)  remove an event-trigger row.
 *   ms-reveal      (change) milestone position radio; data-uid names the form.
 *   set-render-fps (click)  write data-fps into the frame-rate input.
 *   use-suggested-fps (click) write data-fps into the input named by
 *                           data-fps-input and mark it as chosen (#301).
 *   fps-auto-toggle (change) disable the input named by data-fps-input while
 *                           "Auto (suggested)" is ticked.
 *   fps-touched    (input)  note that the user typed a frame rate (#301).
 */
(function () {
  "use strict";

  var actions = window.tlmActions;
  if (!actions) return;

  /* ── Schedule preset ─────────────────────────────────────────────────── */
  function schedReveal(el) {
    var preset = el.value;
    var sunPanel = document.getElementById("sched-sun-panel");
    var customPanel = document.getElementById("sched-custom-panel");
    if (sunPanel) sunPanel.hidden = preset !== "sun";
    if (customPanel) customPanel.hidden = preset !== "custom";
  }

  /* ── Exact-time rows ─────────────────────────────────────────────────── */
  function exactKindChange(selectEl) {
    var row = selectEl.closest(".exact-time-row");
    if (!row) return;
    var timeGroup = row.querySelector("[data-time-group]");
    var timeInput = row.querySelector('input[name="anchor_time"]');
    if (!timeGroup || !timeInput) return;
    var isSolar =
      selectEl.value === "solar_noon" ||
      selectEl.value === "sunrise" ||
      selectEl.value === "sunset";
    timeGroup.hidden = isSolar;
    // A hidden time still POSTs, blank, so the anchor lists stay aligned.
    if (isSolar) timeInput.value = "";
  }

  // Give each cloned label a for= that points at its control's fresh id.
  function relabel(row) {
    row.querySelectorAll("label.form-label").forEach(function (lbl) {
      var group = lbl.closest(".form-group");
      var ctrl = group && group.querySelector("[id]");
      if (ctrl) lbl.setAttribute("for", ctrl.id);
    });
  }

  // Keep the "new:<n>" checkbox values contiguous; saved rows (numeric ids)
  // are untouched.
  function renumberNewRows(container, rowSelector, checkboxName) {
    var n = 0;
    container.querySelectorAll(rowSelector).forEach(function (r) {
      var chk = r.querySelector('input[name="' + checkboxName + '"]');
      if (chk && chk.value.indexOf("new:") === 0) {
        chk.value = "new:" + n;
        n++;
      }
    });
  }

  function exactAddRow() {
    var container = document.getElementById("exact-time-rows");
    var template = document.getElementById("exact-time-row-template");
    var emptyMsg = document.getElementById("exact-time-empty-state");
    if (!container || !template) return null;

    var newIdx = container.querySelectorAll(".exact-time-row").length;
    var fragment = template.content.cloneNode(true);
    var row = fragment.querySelector(".exact-time-row");

    var chk = row.querySelector('input[name="anchor_enabled"]');
    if (chk) chk.value = "new:" + newIdx;

    var uid = "anchor-row-new-" + newIdx;
    var kind = row.querySelector('select[name="anchor_kind"]');
    var time = row.querySelector('input[name="anchor_time"]');
    var offset = row.querySelector('input[name="anchor_offset"]');
    if (kind) kind.id = uid + "-kind";
    if (time) time.id = uid + "-time";
    if (offset) offset.id = uid + "-offset";
    relabel(row);

    container.appendChild(fragment);
    if (emptyMsg) emptyMsg.hidden = true;
    return row;
  }

  function exactRemoveRow(buttonEl) {
    var row = buttonEl.closest(".exact-time-row");
    var container = document.getElementById("exact-time-rows");
    var emptyMsg = document.getElementById("exact-time-empty-state");
    if (!row || !container) return;

    container.removeChild(row);
    renumberNewRows(container, ".exact-time-row", "anchor_enabled");
    var remaining = container.querySelectorAll(".exact-time-row");
    if (emptyMsg && remaining.length === 0) emptyMsg.hidden = false;

    // A removed input fires no event of its own; tell the listeners (the
    // create form's storage pre-flight, the solar preview) with a bubbling
    // change, as an edit would.
    container.dispatchEvent(new Event("change", { bubbles: true }));
  }

  /* ── Event-trigger rows ──────────────────────────────────────────────── */
  function triggerAddRow() {
    var container = document.getElementById("event-trigger-rows");
    var template = document.getElementById("event-trigger-row-template");
    var emptyMsg = document.getElementById("event-trigger-empty-state");
    if (!container || !template) return;

    // Count only new rows: the server indexes "new:<n>" among new rows, so
    // the first row added to a project with saved triggers is "new:0".
    var newIdx = container.querySelectorAll(
      'input[name="trigger_enabled"][value^="new:"]'
    ).length;
    var fragment = template.content.cloneNode(true);
    var row = fragment.querySelector(".event-trigger-row");

    var chk = row.querySelector('input[name="trigger_enabled"]');
    if (chk) chk.value = "new:" + newIdx;

    var uid = "trigger-row-new-" + newIdx;
    var topic = row.querySelector('select[name="trigger_topic"]');
    var cooldown = row.querySelector('input[name="trigger_cooldown"]');
    if (topic) topic.id = uid + "-topic";
    if (cooldown) cooldown.id = uid + "-cooldown";
    relabel(row);

    container.appendChild(fragment);
    if (emptyMsg) emptyMsg.hidden = true;
  }

  function triggerRemoveRow(buttonEl) {
    var row = buttonEl.closest(".event-trigger-row");
    var container = document.getElementById("event-trigger-rows");
    var emptyMsg = document.getElementById("event-trigger-empty-state");
    if (!row || !container) return;

    container.removeChild(row);
    renumberNewRows(container, ".event-trigger-row", "trigger_enabled");
    var remaining = container.querySelectorAll(".event-trigger-row");
    if (emptyMsg && remaining.length === 0) emptyMsg.hidden = false;
  }

  /* ── Capture mode (create form and settings form) ────────────────────── */
  // Show the interval fields or the solar / exact-time fields, and disable the
  // (required) interval controls while hidden so they neither validate nor
  // submit in solar mode.
  function captureModeSync() {
    var checked = document.querySelector('input[name="capture_mode"]:checked');
    var solar = !!(checked && checked.value === "solar");
    document.querySelectorAll(".interval-only-field").forEach(function (el) {
      el.hidden = solar;
    });
    var interval = document.getElementById("capture_interval_value");
    if (interval) interval.disabled = solar;
    var unit = document.querySelector('select[name="capture_interval_unit"]');
    if (unit) unit.disabled = solar;
  }

  function captureModeChange() {
    captureModeSync();
    var checked = document.querySelector('input[name="capture_mode"]:checked');
    if (!checked || checked.value !== "solar") return;
    // A solar project needs at least one capture time: seed one when the list
    // is empty so the operator starts from a ready-to-edit row, defaulting to
    // solar noon (the headline use case) when the camera has a location.
    if (document.querySelectorAll("#exact-time-rows .exact-time-row").length) {
      return;
    }
    var row = exactAddRow();
    var kind = row && row.querySelector('select[name="anchor_kind"]');
    var solarOpt = kind && kind.querySelector('option[value="solar_noon"]');
    if (kind && solarOpt && !solarOpt.disabled) {
      kind.value = "solar_noon";
      exactKindChange(kind); // hide the clock time field
    }
  }

  /* ── Milestone position ──────────────────────────────────────────────── */
  function milestoneReveal(el) {
    var uid = el.getAttribute("data-uid");
    if (!uid) return;
    var framePanel = document.getElementById(uid + "-frame-panel");
    var timePanel = document.getElementById(uid + "-time-panel");
    if (framePanel) framePanel.hidden = el.value !== "frame";
    if (timePanel) timePanel.hidden = el.value !== "time";
  }

  /* ── Render frame-rate suggestion ────────────────────────────────────── */
  function setRenderFps(el) {
    var input = document.getElementById("render_fps");
    var value = el.getAttribute("data-fps");
    if (input && value) input.value = value;
  }

  /* ── Suggested render frame rate (#301) ───────────────────────────────── */
  // A suggestion fills the render form's frame-rate field only while the user
  // has not typed in it; "Use suggested" always restores it.
  function useSuggestedFps(el) {
    var input = document.getElementById(el.getAttribute("data-fps-input"));
    var value = el.getAttribute("data-fps");
    if (!input || !value) return;
    input.value = value;
    input.removeAttribute("data-user-fps");
  }

  function fpsTouched(el) {
    el.setAttribute("data-user-fps", "1");
  }

  function fpsAutoToggle(el) {
    var input = document.getElementById(el.getAttribute("data-fps-input"));
    if (input) input.disabled = el.checked;
  }

  document.addEventListener("htmx:afterSwap", function (event) {
    var target = event.detail && event.detail.target;
    if (!target || target.id !== "trigger-fps-suggest") return;
    var holder = target.querySelector("[data-suggested-fps]");
    var input = document.getElementById("trigger-render-fps");
    var value = holder && holder.getAttribute("data-suggested-fps");
    if (input && value && !input.hasAttribute("data-user-fps")) {
      input.value = value;
    }
  });

  actions.register("use-suggested-fps", "click", useSuggestedFps);
  actions.register("fps-auto-toggle", "change", fpsAutoToggle);
  actions.register("fps-touched", "input", fpsTouched);
  actions.register("sched-reveal", "change", schedReveal);
  actions.register("capture-mode", "change", captureModeChange);
  actions.register("exact-add", "click", exactAddRow);
  actions.register("exact-remove", "click", exactRemoveRow);
  actions.register("exact-kind", "change", exactKindChange);
  actions.register("trigger-add", "click", triggerAddRow);
  actions.register("trigger-remove", "click", triggerRemoveRow);
  actions.register("ms-reveal", "change", milestoneReveal);
  actions.register("set-render-fps", "click", setRenderFps);

  // The inline scripts applied the capture mode as they were parsed, which
  // covered the full page and every swapped-in fragment. htmx.onLoad is called
  // for the document and for each swapped-in element (no eval involved).
  if (window.htmx && typeof window.htmx.onLoad === "function") {
    window.htmx.onLoad(function (elt) {
      if (
        elt &&
        typeof elt.querySelector === "function" &&
        elt.querySelector('input[name="capture_mode"]')
      ) {
        captureModeSync();
      }
    });
  }
})();
