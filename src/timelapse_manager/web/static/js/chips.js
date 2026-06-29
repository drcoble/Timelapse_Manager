/*
 * Suggestion / filter chips.
 *
 * Suggestion chip (writes a value into a sibling form control):
 *   <div class="chip-group" data-chip-target="#interval-value"
 *        data-chip-unit-target="#interval-unit">
 *     <button type="button" class="chip" data-value="5" data-unit="minutes">5m</button>
 *     ...
 *   </div>
 * Clicking sets the target control's value (and optional unit control), marks
 * the chip active (single-select within its group), and fires an `input` +
 * `change` event so any listeners (HTMX, validation) react.
 *
 * Filter chip (multi-select toggle): a .chip with [aria-pressed] toggles its
 * own pressed state; submission/HTMX is left to the consuming markup.
 *
 * Event-delegated, so HTMX-swapped chips work without re-binding.
 */
(function () {
  "use strict";

  function fire(el, type) {
    el.dispatchEvent(new Event(type, { bubbles: true }));
  }

  document.addEventListener("click", function (e) {
    if (!e.target || typeof e.target.closest !== "function") return;
    var chip = e.target.closest(".chip");
    if (!chip) return;

    // Multi-select filter toggle.
    if (chip.hasAttribute("aria-pressed")) {
      var pressed = chip.getAttribute("aria-pressed") === "true";
      chip.setAttribute("aria-pressed", pressed ? "false" : "true");
      return;
    }

    // Suggestion chip: apply value to the group's target control(s).
    var group = chip.closest(".chip-group");
    if (!group) return;
    var targetSel = group.getAttribute("data-chip-target");
    var target = targetSel && document.querySelector(targetSel);
    if (target && chip.hasAttribute("data-value")) {
      target.value = chip.getAttribute("data-value");
      fire(target, "input");
      fire(target, "change");
    }
    var unitSel = group.getAttribute("data-chip-unit-target");
    var unitTarget = unitSel && document.querySelector(unitSel);
    if (unitTarget && chip.hasAttribute("data-unit")) {
      unitTarget.value = chip.getAttribute("data-unit");
      fire(unitTarget, "change");
    }
    group.querySelectorAll(".chip").forEach(function (c) {
      c.classList.toggle("active", c === chip);
    });
  });
})();
