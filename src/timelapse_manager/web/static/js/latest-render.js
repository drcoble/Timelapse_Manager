/*
 * Latest Frame player (#24): click the project page's Latest Frame to play the
 * project's newest render in place.
 *
 * Markup contract (templates/_partials/latest_frame.html, project_detail.html):
 *   - a.latest-frame-play[data-latest-render][data-player="#id"] carries the
 *     #25 playability attributes (data-codec, data-mime, ...); its href is the
 *     render's stream URL, so without JavaScript the link opens the video;
 *   - the player element (#latest-render-player, [data-latest-render-player])
 *     sits beside #latest-frame-container, never inside it, so a swap of the
 *     frame cannot reset a playing video. It holds [data-latest-render-video]
 *     (where the <video> goes), the caption and [data-latest-render-close];
 *   - [data-latest-render-note] is the "can't play" note with the download.
 *
 * Click, Enter or Space runs window.tlmPlayability.check (playability.js):
 *   - plays:     the frame is hidden and a <video controls autoplay muted
 *                playsinline> of the same box plays; "ended" or Close removes
 *                it, shows the frame again and returns focus to it;
 *   - can't:     the note with the download link is shown; there is no
 *                fallback to an older render.
 * Event-delegated, so it needs no re-binding after HTMX swaps.
 */
(function () {
  "use strict";

  function parts(link) {
    var player = document.querySelector(link.getAttribute("data-player") || "");
    if (!player) return null;
    return {
      link: link,
      frame: link.closest("#latest-frame-container") || link.parentNode,
      player: player,
      slot: player.querySelector("[data-latest-render-video]"),
      note: player.parentNode.querySelector("[data-latest-render-note]")
    };
  }

  function close(p) {
    var video = p.slot.querySelector("video");
    if (video) {
      video.pause();
      video.removeAttribute("src");
      video.load();
      video.remove();
    }
    p.player.hidden = true;
    p.frame.hidden = false;
    p.link.focus();
  }

  function play(p) {
    if (p.note) p.note.hidden = true;
    var video = document.createElement("video");
    video.controls = true;
    video.autoplay = true;
    video.muted = true; // timelapses have no audio; muted lets autoplay start
    video.playsInline = true;
    video.setAttribute("playsinline", "");
    video.preload = "auto";
    video.src = p.link.getAttribute("href");
    video.addEventListener("ended", function () { close(p); });
    p.slot.replaceChildren(video);
    p.frame.hidden = true;
    p.player.hidden = false;
    var playing = video.play();
    if (playing && typeof playing.catch === "function") {
      playing.catch(function () { /* the controls let the viewer start it */ });
    }
    video.focus();
  }

  function cantPlay(p) {
    if (p.note) p.note.hidden = false;
  }

  function activate(link) {
    var p = parts(link);
    if (!p || !p.slot) return false;
    var api = window.tlmPlayability;
    if (!api) return false; // no checker: let the link open the file
    api.check(link).then(function (ok) {
      if (ok) play(p); else cantPlay(p);
    });
    return true;
  }

  document.addEventListener("click", function (evt) {
    var close_btn = evt.target.closest && evt.target.closest("[data-latest-render-close]");
    if (close_btn) {
      var player = close_btn.closest("[data-latest-render-player]");
      var link = player && document.querySelector('[data-player="#' + player.id + '"]');
      if (link) {
        var p = parts(link);
        if (p) close(p);
      }
      return;
    }
    var link_el = evt.target.closest && evt.target.closest("a[data-latest-render]");
    if (!link_el || evt.defaultPrevented || evt.button !== 0 ||
        evt.metaKey || evt.ctrlKey || evt.shiftKey || evt.altKey) {
      return;
    }
    if (activate(link_el)) evt.preventDefault();
  });

  // Links activate on Enter natively (as a click); Space needs a handler.
  document.addEventListener("keydown", function (evt) {
    if (evt.key !== " " && evt.key !== "Spacebar") return;
    var link = evt.target.closest && evt.target.closest("a[data-latest-render]");
    if (!link) return;
    evt.preventDefault();
    activate(link);
  });
})();
