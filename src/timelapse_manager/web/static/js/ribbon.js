/*
 * Time-ribbon interactivity.
 *
 * For the interactive variant (.time-ribbon-svg--interactive, used where the
 * ribbon doubles as a scrubber), a click maps its x-position to a timestamp
 * within the ribbon's [data-start, data-end] epoch bounds and dispatches a
 * `ribbon:jump` event ({ fraction, timestampMs }). The frames browser consumes
 * this in a later phase; here we only establish the contract. Event-delegated
 * so HTMX-swapped ribbons work without re-binding.
 */
(function () {
  "use strict";

  document.addEventListener("click", function (e) {
    if (!e.target || typeof e.target.closest !== "function") return;
    var svg = e.target.closest(".time-ribbon-svg--interactive");
    if (!svg) return;
    var wrap = svg.closest(".time-ribbon");
    if (!wrap) return;

    var rect = svg.getBoundingClientRect();
    if (!rect.width) return;
    var fraction = (e.clientX - rect.left) / rect.width;
    fraction = fraction < 0 ? 0 : fraction > 1 ? 1 : fraction;

    var detail = { fraction: fraction };
    var start = parseInt(wrap.getAttribute("data-start"), 10);
    var end = parseInt(wrap.getAttribute("data-end"), 10);
    if (!isNaN(start) && !isNaN(end) && end > start) {
      detail.timestampMs = Math.round((start + fraction * (end - start)) * 1000);
    }
    wrap.dispatchEvent(
      new CustomEvent("ribbon:jump", { detail: detail, bubbles: true })
    );
  });
})();
