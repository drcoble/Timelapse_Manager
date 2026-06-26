/*
 * Client-side tab toggle. No server round-trip; all panels are rendered and
 * shown/hidden in place.
 *
 * Markup:
 *   <div class="tabs" role="tablist">
 *     <button class="tab-item active" data-tab-target="#tab-a" aria-selected="true">A</button>
 *     <button class="tab-item"        data-tab-target="#tab-b" aria-selected="false">B</button>
 *   </div>
 *   <div id="tab-a" class="tab-panel" role="tabpanel">…</div>
 *   <div id="tab-b" class="tab-panel" role="tabpanel" hidden>…</div>
 *
 * Panels controlled by one tablist must be siblings. Event-delegated so
 * HTMX-swapped tabs work without re-binding.
 */
(function () {
  "use strict";

  document.addEventListener("click", function (e) {
    if (!e.target || typeof e.target.closest !== "function") return;
    var tab = e.target.closest(".tab-item[data-tab-target]");
    if (!tab) return;
    var tablist = tab.closest(".tabs");
    if (!tablist) return;
    var panel = document.querySelector(tab.getAttribute("data-tab-target"));
    if (!panel) return;

    tablist.querySelectorAll(".tab-item").forEach(function (t) {
      var on = t === tab;
      t.classList.toggle("active", on);
      t.setAttribute("aria-selected", on ? "true" : "false");
    });

    var parent = panel.parentNode;
    if (!parent) return;
    Array.prototype.forEach.call(
      parent.querySelectorAll(".tab-panel"),
      function (p) {
        if (p.parentNode === parent) {
          p.hidden = p !== panel;
        }
      }
    );
  });
})();
