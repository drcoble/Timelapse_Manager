/*
 * Clickable table rows: one shared helper for the camera list (#302) and the
 * projects list (#317).
 *
 * A table body opts in with data-row-click="<selector>", the selector of the
 * row's own opener inside a row: the real <button> (cameras) or <a href>
 * (projects) that carries the action, so the keyboard path (Tab, Enter) and the
 * no-JS fallback need no script. This adds the mouse path on top: one delegated
 * click listener on the document forwards a click on the rest of the row to that
 * opener, so it does exactly what a click on the opener does. Because the
 * listener sits on the document, a row that htmx adds or swaps (an edit save)
 * works at once with no re-binding (#265, #270). A row without an opener (a
 * viewer's camera row) does nothing.
 *
 * Left alone: any control in the row (the actions menu and its items, links,
 * buttons, form fields), modified and non-primary clicks (middle-click and
 * Ctrl/Cmd-click stay with the opener link and open a new tab), and a click that
 * ends a text selection (a drag over a cell must not open the row).
 */
(function () {
  "use strict";

  var ROW = "[data-row-click] > tr";
  var CONTROL =
    "a, button, input, select, textarea, label, summary, [role=menuitem], " +
    ".row-actions-menu";

  function selectionActive() {
    var sel = window.getSelection && window.getSelection();
    return !!sel && !sel.isCollapsed && String(sel).length > 0;
  }

  document.addEventListener("click", function (e) {
    if (e.defaultPrevented || e.button !== 0) return;
    if (e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return;
    var target = e.target;
    if (!target || !target.closest) return;
    var row = target.closest(ROW);
    if (!row || target.closest(CONTROL)) return;
    if (selectionActive()) return;
    var selector = row.parentElement.getAttribute("data-row-click");
    var opener = selector ? row.querySelector(selector) : null;
    if (opener) opener.click();
  });
})();
