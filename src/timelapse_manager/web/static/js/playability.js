/*
 * Per-browser playback check (#25): the browser, not the server, decides
 * which renders play inline.
 *
 * Markup contract (see templates/_partials/_playback.html):
 *   - slot:  any element with [data-playability] and
 *            data-codec   RFC 6381 codec string ("" when not recorded)
 *            data-mime    container MIME type ("video/mp4", "video/webm", ...)
 *            data-width / data-height / data-fps / data-bitrate (optional)
 *   - [data-play-ok] inside the slot: shown when this browser can play it
 *   - [data-play-no] inside the slot: shown when it can't
 *
 * The check: navigator.mediaCapabilities.decodingInfo({type: "file", ...})
 * when the codec string and every size/rate input are known (it accounts for
 * resolution and, on Safari, for a hardware decoder); otherwise, or when it
 * throws, HTMLMediaElement.canPlayType ("probably"/"maybe" = yes). Results are
 * cached per codec + MIME + size in sessionStorage, read synchronously so a
 * polled list that re-renders every few seconds does not flicker.
 *
 * Without JavaScript nothing here runs and the server's markup stands: the
 * player shows only for H.264/MP4 renders, and every render keeps its
 * Download link.
 *
 * window.tlmPlayability.check(el) returns a Promise<boolean> for one slot, so
 * other scripts (the Latest Frame player, #24) reuse the same check.
 */
(function () {
  "use strict";

  var CACHE_PREFIX = "tlm-playable:";

  function intAttr(el, name) {
    var n = parseInt(el.getAttribute(name) || "", 10);
    return n > 0 ? n : null;
  }

  function floatAttr(el, name) {
    var n = parseFloat(el.getAttribute(name) || "");
    return n > 0 ? n : null;
  }

  function configOf(el) {
    return {
      codec: el.getAttribute("data-codec") || "",
      mime: el.getAttribute("data-mime") || "",
      width: intAttr(el, "data-width"),
      height: intAttr(el, "data-height"),
      fps: floatAttr(el, "data-fps"),
      bitrate: intAttr(el, "data-bitrate")
    };
  }

  function contentType(cfg) {
    return cfg.codec ? cfg.mime + '; codecs="' + cfg.codec + '"' : cfg.mime;
  }

  function cacheKey(cfg) {
    return CACHE_PREFIX + [cfg.mime, cfg.codec, cfg.width, cfg.height].join("|");
  }

  // sessionStorage can be missing or throw (private mode, blocked storage).
  function readCache(cfg) {
    try {
      var v = window.sessionStorage.getItem(cacheKey(cfg));
      if (v === "1") return true;
      if (v === "0") return false;
    } catch (e) { /* no cache: just ask again */ }
    return null;
  }

  function writeCache(cfg, ok) {
    try {
      window.sessionStorage.setItem(cacheKey(cfg), ok ? "1" : "0");
    } catch (e) { /* best effort */ }
  }

  function canPlayTypeCheck(cfg) {
    var video = document.createElement("video");
    if (typeof video.canPlayType !== "function") return false;
    var answer = video.canPlayType(contentType(cfg));
    return answer === "probably" || answer === "maybe";
  }

  function decodingInfoCheck(cfg) {
    var mc = navigator.mediaCapabilities;
    if (!cfg.codec || !cfg.width || !cfg.height || !cfg.fps || !cfg.bitrate ||
        !mc || typeof mc.decodingInfo !== "function") {
      return null;
    }
    try {
      return mc.decodingInfo({
        type: "file",
        video: {
          contentType: contentType(cfg),
          width: cfg.width,
          height: cfg.height,
          bitrate: cfg.bitrate,
          framerate: cfg.fps
        }
      }).then(function (info) {
        return !!(info && info.supported);
      }, function () {
        return canPlayTypeCheck(cfg);
      });
    } catch (e) {
      return null;
    }
  }

  // Resolve whether this browser can play the slot's render.
  function decide(cfg) {
    if (!cfg.mime) return Promise.resolve(false);
    var cached = readCache(cfg);
    if (cached !== null) return Promise.resolve(cached);
    var pending = decodingInfoCheck(cfg);
    if (pending === null) pending = Promise.resolve(canPlayTypeCheck(cfg));
    return pending.then(function (ok) {
      writeCache(cfg, ok);
      return ok;
    });
  }

  function apply(slot, ok) {
    slot.setAttribute("data-playable", ok ? "yes" : "no");
    var i;
    var yes = slot.querySelectorAll("[data-play-ok]");
    for (i = 0; i < yes.length; i++) yes[i].hidden = !ok;
    var no = slot.querySelectorAll("[data-play-no]");
    for (i = 0; i < no.length; i++) no[i].hidden = ok;
  }

  function checkSlot(slot) {
    var cfg = configOf(slot);
    // A cached answer is applied synchronously, in the same task as the swap.
    var cached = cfg.mime ? readCache(cfg) : false;
    if (cached !== null) {
      apply(slot, cached);
      return;
    }
    decide(cfg).then(function (ok) { apply(slot, ok); });
  }

  function scan(root) {
    var scope = root && root.querySelectorAll ? root : document;
    if (scope.matches && scope.matches("[data-playability]")) checkSlot(scope);
    var slots = scope.querySelectorAll("[data-playability]");
    for (var i = 0; i < slots.length; i++) checkSlot(slots[i]);
  }

  window.tlmPlayability = {
    check: function (el) { return decide(configOf(el)); },
    scan: scan
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { scan(document); });
  } else {
    scan(document);
  }
  // Rows and cards swapped in by HTMX (the Renders list refresh, the card
  // dialog) are checked as they settle.
  document.addEventListener("htmx:afterSettle", function (evt) {
    scan(evt.target);
  });
})();
