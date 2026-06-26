/*
 * Right-side drawer controller.
 *
 * Markup contract:
 *   - opener:  <a href="/fallback" data-drawer-open="#drawer-main"
 *                 hx-get="/fragment" hx-target="#drawer-main .drawer-body"
 *                 hx-swap="innerHTML" data-drawer-title="Title"> ... </a>
 *              (the href is the no-JS fallback; with JS, HTMX loads the fragment
 *               into the drawer body and the drawer opens once it has swapped in)
 *   - drawer:  <aside id="drawer-main" class="drawer" role="dialog"
 *                     aria-modal="true" aria-hidden="true"
 *                     aria-labelledby="drawer-title">
 *                <header class="drawer-header"><h2 id="drawer-title"></h2>
 *                  <button data-drawer-close>…</button></header>
 *                <div class="drawer-body"></div></aside>
 *   - backdrop: <div class="drawer-backdrop" data-drawer-backdrop></div>
 *   - close:   any element inside the drawer with [data-drawer-close]
 *
 * While open, the nav and main regions are made `inert` + aria-hidden so the
 * modal semantics are honest, the page behind is scroll-locked, focus is
 * trapped inside the drawer, and Escape or a backdrop click closes it. Focus is
 * restored to the opener on close. An opener that loads its body via HTMX opens
 * the drawer on `htmx:afterSwap` (so the panel never flashes empty); an opener
 * with static content opens immediately on click. Event-delegated, so
 * HTMX-swapped openers/drawers work without re-binding.
 */
(function () {
  "use strict";

  var FOCUSABLE =
    'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

  var lastOpener = null;
  var openDrawerEl = null;
  var pendingOpen = null; // {drawer, opener} awaiting an HTMX body load
  var scrollbarComp = false;

  function backdropFor(drawer) {
    return (
      document.querySelector(
        '.drawer-backdrop[data-drawer-for="' + drawer.id + '"]'
      ) || document.querySelector(".drawer-backdrop[data-drawer-backdrop]")
    );
  }

  function setOutsideInert(on) {
    document.querySelectorAll(".app-nav, .app-main").forEach(function (el) {
      if (on) {
        el.setAttribute("inert", "");
        el.setAttribute("aria-hidden", "true");
      } else {
        el.removeAttribute("inert");
        el.removeAttribute("aria-hidden");
      }
    });
  }

  function lockScroll(on) {
    var body = document.body;
    if (on) {
      var sw = window.innerWidth - document.documentElement.clientWidth;
      if (sw > 0) {
        body.style.paddingRight = sw + "px";
        scrollbarComp = true;
      }
      body.classList.add("drawer-open");
    } else {
      body.classList.remove("drawer-open");
      if (scrollbarComp) {
        body.style.paddingRight = "";
        scrollbarComp = false;
      }
    }
  }

  function bodyOf(drawer) {
    return drawer.querySelector(".drawer-body") || drawer;
  }

  function firstVisible(scope) {
    var els = scope.querySelectorAll(FOCUSABLE);
    for (var i = 0; i < els.length; i++) {
      // Skip non-visible candidates (e.g. the hidden CSRF input) — `.focus()`
      // on a hidden field is a no-op and would swallow the intended focus.
      if (els[i].offsetParent !== null) return els[i];
    }
    return null;
  }

  function focusFirstField(drawer) {
    var first = firstVisible(bodyOf(drawer)) || firstVisible(drawer);
    if (first) first.focus();
  }

  function applyTitle(drawer, opener) {
    var titleEl = drawer.querySelector("#drawer-title, .drawer-title");
    if (!titleEl) return;
    var frag = bodyOf(drawer).firstElementChild;
    var t =
      (frag && frag.getAttribute && frag.getAttribute("data-drawer-title")) ||
      (opener && opener.getAttribute && opener.getAttribute("data-drawer-title"));
    if (t) titleEl.textContent = t;
  }

  function open(drawer, opener) {
    if (!drawer) return;
    if (openDrawerEl === drawer) {
      // Body re-swapped while already open (e.g. an inline step change) — just
      // refresh the title and move focus to the first field. Do not re-lock.
      applyTitle(drawer, opener || lastOpener);
      focusFirstField(drawer);
      return;
    }
    if (openDrawerEl) return; // a different drawer is already open
    openDrawerEl = drawer;
    lastOpener = opener || lastOpener || null;
    var backdrop = backdropFor(drawer);
    drawer.classList.add("open");
    drawer.setAttribute("aria-hidden", "false");
    if (backdrop) backdrop.classList.add("open");
    setOutsideInert(true);
    lockScroll(true);
    applyTitle(drawer, lastOpener);
    focusFirstField(drawer);
    document.addEventListener("keydown", onKeydown, true);
  }

  function close() {
    var drawer = openDrawerEl;
    pendingOpen = null;
    if (!drawer) return;
    var backdrop = backdropFor(drawer);
    drawer.classList.remove("open");
    drawer.setAttribute("aria-hidden", "true");
    if (backdrop) backdrop.classList.remove("open");
    setOutsideInert(false);
    lockScroll(false);
    document.removeEventListener("keydown", onKeydown, true);
    openDrawerEl = null;
    if (lastOpener && typeof lastOpener.focus === "function") {
      lastOpener.focus();
    }
    lastOpener = null;
  }

  function onKeydown(e) {
    if (!openDrawerEl) return;
    if (e.key === "Escape") {
      e.preventDefault();
      close();
      return;
    }
    if (e.key !== "Tab") return;
    var items = Array.prototype.filter.call(
      openDrawerEl.querySelectorAll(FOCUSABLE),
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
    var opener = e.target.closest("[data-drawer-open]");
    if (opener) {
      var sel = opener.getAttribute("data-drawer-open");
      var drawer = sel && document.querySelector(sel);
      if (drawer) {
        var loadsContent =
          opener.hasAttribute("hx-get") || opener.hasAttribute("data-hx-get");
        if (loadsContent) {
          // Let HTMX perform the GET; open once the body has swapped in.
          pendingOpen = { drawer: drawer, opener: opener };
        } else {
          e.preventDefault();
          open(drawer, opener);
        }
      }
      return;
    }
    if (e.target.closest("[data-drawer-close]")) {
      e.preventDefault();
      close();
      return;
    }
    if (
      openDrawerEl &&
      e.target.classList &&
      e.target.classList.contains("drawer-backdrop")
    ) {
      close();
    }
  });

  // Complete a deferred open (or refresh an open drawer) once HTMX has swapped
  // content into the drawer body.
  document.body.addEventListener("htmx:afterSwap", function (e) {
    var tgt = e.target;
    if (!tgt || typeof tgt.closest !== "function") return;
    var drawer = tgt.closest(".drawer");
    if (!drawer) return;
    if (pendingOpen && pendingOpen.drawer === drawer) {
      var op = pendingOpen.opener;
      pendingOpen = null;
      open(drawer, op);
    } else if (openDrawerEl === drawer) {
      applyTitle(drawer, lastOpener);
      focusFirstField(drawer);
    }
  });
})();
