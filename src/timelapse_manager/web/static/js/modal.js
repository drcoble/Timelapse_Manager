/*
 * Centered modal controller — a contained dialog, distinct from the side
 * drawer. Used for self-contained actions (e.g. camera Discover) that are not
 * a create/detail flow, and for the render/export card (job_card_modal.html).
 *
 * Markup contract:
 *   - opener:   <button data-modal-open="#discover-modal"> ... </button>
 *   - backdrop: <div class="modal-backdrop" data-modal-for="discover-modal">
 *                 <div id="discover-modal" class="discover-modal" role="dialog"
 *                      aria-modal="true" aria-hidden="true"
 *                      aria-labelledby="…"> ... </div>
 *               </div>
 *   - close:    any element inside the modal with [data-modal-close], or a
 *               "modal:close" event on the document (an HTMX response sends
 *               it with `HX-Trigger: modal:close`)
 *   - content:  optional [data-modal-content] element inside the modal whose
 *               HTMX-loaded content is reset to its original placeholder each
 *               time the modal opens or closes (so a previous card, or its
 *               playing video, never lingers)
 *
 * While open: the page behind is scroll-locked, focus is trapped inside the
 * dialog, the dialog's aria-hidden flips to "false", and Escape or a click on
 * the backdrop (outside the dialog) closes it. Focus is restored to the opener
 * on close -- or, when the opener was re-rendered meanwhile (a polled list
 * swapped it out), to the element that now carries the opener's id. Unlike the
 * drawer, this controller does NOT make ancestor regions
 * `inert` — the modal lives inside the page content, so inerting an ancestor
 * would inert the modal itself. Event-delegated, so HTMX-swapped openers and
 * modals work without re-binding.
 */
(function () {
  "use strict";

  // <summary> and media controls are tab stops too (the render card's Play /
  // Delete disclosures and its <video>); leaving them out would let Tab walk
  // out of the dialog.
  var FOCUSABLE =
    'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), summary, video[controls], [tabindex]:not([tabindex="-1"])';

  var lastOpener = null;
  var lastOpenerId = null;
  var lastFallback = null;
  var openModalEl = null;
  var scrollbarComp = false;
  // Original placeholder markup of each [data-modal-content] element, keyed by
  // the element itself (captured the first time its modal opens).
  var placeholders = new WeakMap();

  function resetContent(modal) {
    var content = modal.querySelector("[data-modal-content]");
    if (!content) return;
    if (!placeholders.has(content)) {
      placeholders.set(content, content.innerHTML);
      return;
    }
    content.innerHTML = placeholders.get(content);
  }

  function restoreFocus() {
    var target = lastOpener;
    if (target && !target.isConnected && lastOpenerId) {
      target = document.getElementById(lastOpenerId);
    }
    if (target && target.isConnected && typeof target.focus === "function") {
      target.focus();
      return;
    }
    // The opener is gone for good (its row was deleted): land on the title of
    // the card that held the list instead of dropping focus to <body>.
    var title = lastFallback && lastFallback.isConnected
      ? lastFallback.querySelector(".card-title, h1, h2")
      : null;
    if (title) {
      if (!title.hasAttribute("tabindex")) title.setAttribute("tabindex", "-1");
      title.focus();
    }
  }

  function backdropFor(modal) {
    return (
      modal.closest(".modal-backdrop") ||
      document.querySelector('.modal-backdrop[data-modal-for="' + modal.id + '"]')
    );
  }

  function lockScroll(on) {
    var body = document.body;
    if (on) {
      var sw = window.innerWidth - document.documentElement.clientWidth;
      if (sw > 0) {
        body.style.paddingRight = sw + "px";
        scrollbarComp = true;
      }
      body.classList.add("modal-open");
    } else {
      body.classList.remove("modal-open");
      if (scrollbarComp) {
        body.style.paddingRight = "";
        scrollbarComp = false;
      }
    }
  }

  // Rendered and reachable. checkVisibility() also sees through a closed
  // <details> (Chrome hides its content with content-visibility, which leaves
  // offsetParent set); offsetParent is the fallback for older browsers.
  function isVisible(el) {
    // visibilityProperty: a control still mid-transition to visible (or one
    // hidden with visibility: hidden) cannot take focus, so it must not count
    // as the trap's first or last stop.
    if (typeof el.checkVisibility === "function") {
      return el.checkVisibility({ visibilityProperty: true });
    }
    return el.offsetParent !== null;
  }

  function firstVisible(scope) {
    var els = scope.querySelectorAll(FOCUSABLE);
    for (var i = 0; i < els.length; i++) {
      // Skip non-visible candidates (e.g. a hidden CSRF input) — `.focus()` on
      // a hidden field is a no-op and would swallow the intended focus.
      if (isVisible(els[i])) return els[i];
    }
    return null;
  }

  function focusFirstField(modal) {
    var first = firstVisible(modal);
    if (first) first.focus();
  }

  function open(modal, opener) {
    if (!modal || openModalEl) return;
    openModalEl = modal;
    lastOpener = opener || lastOpener || null;
    lastOpenerId = lastOpener && lastOpener.id ? lastOpener.id : null;
    lastFallback = lastOpener && lastOpener.closest ? lastOpener.closest(".card") : null;
    resetContent(modal);
    var backdrop = backdropFor(modal);
    if (backdrop) backdrop.classList.add("open");
    modal.setAttribute("aria-hidden", "false");
    lockScroll(true);
    focusFirstField(modal);
    document.addEventListener("keydown", onKeydown, true);
  }

  function close() {
    var modal = openModalEl;
    if (!modal) return;
    var backdrop = backdropFor(modal);
    if (backdrop) backdrop.classList.remove("open");
    modal.setAttribute("aria-hidden", "true");
    lockScroll(false);
    document.removeEventListener("keydown", onKeydown, true);
    openModalEl = null;
    resetContent(modal);
    restoreFocus();
    lastOpener = null;
    lastOpenerId = null;
    lastFallback = null;
  }

  function onKeydown(e) {
    if (!openModalEl) return;
    if (e.key === "Escape") {
      e.preventDefault();
      close();
      return;
    }
    if (e.key !== "Tab") return;
    var items = Array.prototype.filter.call(
      openModalEl.querySelectorAll(FOCUSABLE),
      function (el) {
        return isVisible(el) || el === document.activeElement;
      }
    );
    if (!items.length) return;
    var first = items[0];
    var last = items[items.length - 1];
    // Focus that slipped out (e.g. its element was swapped away) comes back in.
    if (!openModalEl.contains(document.activeElement)) {
      e.preventDefault();
      (e.shiftKey ? last : first).focus();
      return;
    }
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  }

  // Content loaded into the open modal replaces whatever had focus (the
  // placeholder's close button): put focus back on the first control inside.
  document.addEventListener("htmx:afterSwap", function (e) {
    if (!openModalEl || !e.target || !openModalEl.contains(e.target)) return;
    if (!openModalEl.contains(document.activeElement)) {
      focusFirstField(openModalEl);
    }
  });

  // Closed from a server response (HX-Trigger: modal:close), e.g. after the
  // card's Delete succeeded.
  document.addEventListener("modal:close", function () {
    close();
  });

  document.addEventListener("click", function (e) {
    if (!e.target || typeof e.target.closest !== "function") return;
    var opener = e.target.closest("[data-modal-open]");
    if (opener) {
      var sel = opener.getAttribute("data-modal-open");
      var modal = sel && document.querySelector(sel);
      if (modal) {
        e.preventDefault();
        open(modal, opener);
      }
      return;
    }
    if (e.target.closest("[data-modal-close]")) {
      e.preventDefault();
      close();
      return;
    }
    // A click on the backdrop itself (outside the dialog) closes the modal.
    if (
      openModalEl &&
      e.target.classList &&
      e.target.classList.contains("modal-backdrop")
    ) {
      close();
    }
  });
})();
