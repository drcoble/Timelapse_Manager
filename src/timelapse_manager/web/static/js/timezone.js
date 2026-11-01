/*
  Viewer time zone: browser auto-detect plus the shared client helpers.

  The server-rendered stored zone is passed in via <html data-viewer-tz> (empty
  when none is stored), so no inline script is needed (#70 F-S3).

  Auto-detect (#70 F-R4): when NO zone is stored, POST the browser's zone with
  source=auto, which the server applies only to an empty preference. A stored
  zone, chosen by hand or detected earlier, wins over the browser's; the
  Preferences page offers a one-click switch when the two differ.

  Helpers (#70 F-R5), used by scrubber.js and selection.js, which load after this
  file:

    tlmViewerTz()            The zone every timestamp on the page is shown in:
                             the stored zone, or "UTC" when none is stored or
                             the browser cannot use it. The server falls back to
                             UTC the same way, so the two always agree.
    tlmLocalInputValue(sec)  "YYYY-MM-DDTHH:MM" for a datetime-local input, in
                             the viewer's zone (the server reads a naive value
                             in that zone).
    tlmBrowserTz()           The browser's own zone, or null.
*/
(function () {
  "use strict";

  var validated = Object.create(null);

  function usable(tz) {
    if (!tz || typeof tz !== "string") return false;
    if (tz in validated) return validated[tz];
    var ok = true;
    try {
      // An unknown zone throws RangeError; never let that reach a caller.
      new Intl.DateTimeFormat("en-US", { timeZone: tz });
    } catch (e) {
      ok = false;
    }
    validated[tz] = ok;
    return ok;
  }

  function storedTz() {
    var root = document.documentElement;
    var attr = root && root.getAttribute("data-viewer-tz");
    if (attr) return attr;
    return null;
  }

  function tlmViewerTz() {
    var tz = storedTz();
    return usable(tz) ? tz : "UTC";
  }

  function tlmBrowserTz() {
    try {
      return Intl.DateTimeFormat().resolvedOptions().timeZone || null;
    } catch (e) {
      return null;
    }
  }

  function tlmLocalInputValue(epochSeconds) {
    var fmt = new Intl.DateTimeFormat("en-US", {
      timeZone: tlmViewerTz(),
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23"
    });
    var parts = {};
    fmt.formatToParts(new Date(epochSeconds * 1000)).forEach(function (p) {
      parts[p.type] = p.value;
    });
    // Some engines still print midnight as "24" under h23; normalise it.
    var hour = parts.hour === "24" ? "00" : parts.hour;
    return (
      parts.year + "-" + parts.month + "-" + parts.day + "T" + hour + ":" + parts.minute
    );
  }

  window.tlmViewerTz = tlmViewerTz;
  window.tlmBrowserTz = tlmBrowserTz;
  window.tlmLocalInputValue = tlmLocalInputValue;

  // --- Auto-detect: fill an empty preference only ---------------------------
  if (storedTz()) return;
  var detected = tlmBrowserTz();
  if (!detected) return;
  var csrf = document.querySelector('meta[name="csrf-token"]');
  if (!csrf || !csrf.getAttribute("content")) return;
  fetch("/account/timezone", {
    method: "POST",
    headers: {
      "Content-Type": "application/x-www-form-urlencoded",
      "X-CSRF-Token": csrf.getAttribute("content")
    },
    body: "source=auto&timezone=" + encodeURIComponent(detected)
  }).catch(function () {});
})();
