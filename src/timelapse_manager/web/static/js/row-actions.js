/*
 * Row-actions popover placement (#210).
 *
 * The menu itself is CSS-only (row-actions.css): it opens on hover and on
 * :focus-within, with no script. Its popover is absolutely positioned inside the
 * table's card wrapper, and that wrapper scrolls (overflow-x: auto), which clips
 * anything that hangs below the last rows. When script runs, the open popover is
 * taken out of that clipping by placing it with position: fixed beside its
 * trigger, flipped above the trigger when there is no room below. Without script
 * the popover keeps its CSS placement, so the menu still opens.
 *
 * Placement is recomputed whenever a menu opens, and while one is open on scroll
 * and resize.
 */
(function () {
  "use strict";

  var MENU = ".row-actions-menu";
  var OPEN = ".row-actions-menu:hover, .row-actions-menu:focus-within";
  var MARGIN = 4;

  function place(menu) {
    var popover = menu.querySelector(".row-actions-popover");
    var trigger = menu.querySelector(".row-actions-trigger") || menu;
    if (!popover) return;
    // Fixed positioning is what escapes the card wrapper's overflow clipping.
    popover.style.position = "fixed";
    popover.style.right = "auto";
    popover.style.bottom = "auto";
    // The CSS has already shown the popover (hover / focus-within), so it can be
    // measured here.
    var t = trigger.getBoundingClientRect();
    var width = popover.offsetWidth;
    var height = popover.offsetHeight;
    var viewW = document.documentElement.clientWidth;
    var viewH = document.documentElement.clientHeight;

    var left = Math.min(t.right - width, viewW - width - MARGIN);
    popover.style.left = Math.max(MARGIN, left) + "px";

    var top = t.bottom;
    if (top + height > viewH - MARGIN && t.top - height >= MARGIN) {
      top = t.top - height; // not enough room below: open upward
    } else if (top + height > viewH - MARGIN) {
      top = Math.max(MARGIN, viewH - height - MARGIN);
    }
    popover.style.top = top + "px";
  }

  function menuOf(node) {
    return node && node.closest ? node.closest(MENU) : null;
  }

  function onOpen(e) {
    var menu = menuOf(e.target);
    if (menu) place(menu);
  }

  function reposition() {
    var open = document.querySelectorAll(OPEN);
    for (var i = 0; i < open.length; i++) place(open[i]);
  }

  document.addEventListener("mouseover", onOpen);
  document.addEventListener("focusin", onOpen);
  window.addEventListener("resize", reposition);
  document.addEventListener("scroll", reposition, true);
})();
