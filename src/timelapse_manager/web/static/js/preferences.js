/*
  Preferences page: live theme preview, time-zone auto-detect button, the
  "Your browser reports X" hint, and Save.

  Moved out of account_preferences.html's inline <script> (#70 F-R2, F-R4).
  Loaded with `defer`, so net.js (tlmRequest/tlmExclusive), htmx-errors.js
  (tlmToast) and timezone.js (tlmBrowserTz) at the end of base.html's body have
  already run.

  Save posts the theme and the zone (source=manual, so the server replaces the
  stored zone; a blank clears it) and reloads only when BOTH succeed. A failure
  leaves the page as it is, and net.js shows the reason in a toast; no alert().
*/
(function () {
  "use strict";

  var tzInput = document.getElementById("timezone-input");

  function selectedTheme() {
    var checked = document.querySelector("#theme-segment input:checked");
    return checked ? checked.value : "system";
  }

  function applyTheme(t) {
    if (t === "system") {
      document.documentElement.removeAttribute("data-theme");
    } else {
      document.documentElement.setAttribute("data-theme", t);
    }
    try {
      localStorage.setItem("theme", t);
    } catch (e) {}
  }

  function browserTz() {
    return typeof window.tlmBrowserTz === "function" ? window.tlmBrowserTz() : null;
  }

  // Live-apply the theme as the user picks a segment.
  var seg = document.getElementById("theme-segment");
  if (seg) {
    seg.addEventListener("change", function () {
      applyTheme(selectedTheme());
    });
  }

  // Auto-detect fills the field from the browser; Save still has to be clicked.
  var detect = document.getElementById("timezone-detect-btn");
  if (detect && tzInput) {
    detect.addEventListener("click", function () {
      var tz = browserTz();
      if (tz) tzInput.value = tz;
    });
  }

  function save(trigger) {
    if (typeof window.tlmExclusive !== "function" || !tzInput) return;
    var theme = selectedTheme();
    var timezone = tzInput.value.trim();
    window.tlmExclusive("preferences-save", trigger, function () {
      return Promise.all([
        window.tlmRequest("preferences-theme", {
          url: "/account/theme",
          method: "POST",
          body: { theme: theme }
        }),
        window.tlmRequest("preferences-timezone", {
          url: "/account/timezone",
          method: "POST",
          body: { source: "manual", timezone: timezone }
        })
      ]).then(
        function () {
          applyTheme(theme);
          window.location.reload();
        },
        function () {
          // net.js has already shown the failure as a toast; stay on the page
          // so the user's choices are not lost.
        }
      );
    });
  }

  var btn = document.getElementById("save-preferences-btn");
  if (btn) {
    btn.addEventListener("click", function () {
      save(btn);
    });
  }

  // "Your browser reports X. Use it?" when the stored zone differs (#70 F-R4).
  // A stored zone wins over the browser's, so this is the one-click switch.
  var hint = document.getElementById("timezone-browser-hint");
  var stored = tzInput ? tzInput.defaultValue.trim() : "";
  var detected = browserTz();
  if (hint && stored && detected && detected !== stored) {
    var name = hint.querySelector("[data-browser-tz]");
    if (name) name.textContent = detected;
    hint.hidden = false;
    var use = document.getElementById("timezone-use-browser-btn");
    if (use) {
      use.addEventListener("click", function () {
        tzInput.value = detected;
        save(use);
      });
    }
  }
})();
