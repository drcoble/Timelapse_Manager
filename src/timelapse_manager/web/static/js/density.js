/*
 * Frame-grid display density. Comfortable / Compact / Filmstrip is a purely
 * cosmetic, client-only choice: it sets data-density on #frame-grid (the CSS
 * does the rest) and remembers the choice in localStorage. No server trip, so
 * it survives across visits without a preference column. The grid div itself
 * persists across continuous-scroll and jump swaps (only its innerHTML is
 * replaced), so the attribute does not need re-applying after a batch load.
 */
(function () {
  "use strict";

  var KEY = "frameDensity";
  var VALID = { comfortable: 1, compact: 1, filmstrip: 1 };

  function grid() {
    return document.getElementById("frame-grid");
  }

  function apply(value) {
    var g = grid();
    if (g) g.setAttribute("data-density", value);
  }

  function stored() {
    var v = null;
    try {
      v = window.localStorage.getItem(KEY);
    } catch (e) {
      v = null;
    }
    return v && VALID[v] ? v : "comfortable";
  }

  function syncRadio(value) {
    var r = document.querySelector(
      'input[name="frame-density"][value="' + value + '"]'
    );
    if (r) r.checked = true;
  }

  function init() {
    var value = stored();
    syncRadio(value);
    apply(value);
  }

  document.addEventListener("change", function (e) {
    if (!e.target || e.target.name !== "frame-density") return;
    var value = e.target.value;
    if (!VALID[value]) return;
    apply(value);
    try {
      window.localStorage.setItem(KEY, value);
    } catch (err) {
      /* private mode / storage disabled — the in-page choice still applies */
    }
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
