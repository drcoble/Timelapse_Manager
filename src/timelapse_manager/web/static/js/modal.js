/*
 * Centered modal controller — a contained dialog, distinct from the side
 * drawer. Used for self-contained actions (e.g. camera Discover) that are not
 * a create/detail flow.
 *
 * Markup contract:
 *   - opener:   <button data-modal-open="#discover-modal"> ... </button>
 *   - backdrop: <div class="modal-backdrop" data-modal-for="discover-modal">
 *                 <div id="discover-modal" class="discover-modal" role="dialog"
 *                      aria-modal="true" aria-hidden="true"
 *                      aria-labelledby="…"> ... </div>
 *               </div>
 *   - close:    any element inside the modal with [data-modal-close]
 *
 * While open: the page behind is scroll-locked, focus is trapped inside the
 * dialog, the dialog's aria-hidden flips to "false", and Escape or a click on
 * the backdrop (outside the dialog) closes it. Focus is restored to the opener
 * on close. Unlike the drawer, this controller does NOT make ancestor regions
 * `inert` — the modal lives inside the page content, so inerting an ancestor
 * would inert the modal itself. Event-delegated, so HTMX-swapped openers and
 * modals work without re-binding.
 */
(function () {
  "use strict";

  var FOCUSABLE =
    'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

  var lastOpener = null;
  var openModalEl = null;
  var scrollbarComp = false;

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

  function firstVisible(scope) {
    var els = scope.querySelectorAll(FOCUSABLE);
    for (var i = 0; i < els.length; i++) {
      // Skip non-visible candidates (e.g. a hidden CSRF input) — `.focus()` on
      // a hidden field is a no-op and would swallow the intended focus.
      if (els[i].offsetParent !== null) return els[i];
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
    if (lastOpener && typeof lastOpener.focus === "function") {
      lastOpener.focus();
    }
    lastOpener = null;
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
        return el.offsetParent !== null || el === document.activeElement;
      }
    );
    if (!items.length) return;
    var first = items[0];
    var last = items[items.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  }

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
