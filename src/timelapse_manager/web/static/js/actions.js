/*
 * Delegated action registry (#70 F-S3, #67-S17).
 *
 * Markup says WHAT to do; script says HOW. An element carries
 * `data-action="<name>"` plus any `data-*` parameters the action needs:
 *
 *   <button type="button" data-action="exact-add">Add</button>
 *   <input type="radio" data-action="sched-reveal" value="sun">
 *
 * A module registers a handler for one DOM event type per action name:
 *
 *   window.tlmActions.register("exact-add", "click", function (el, event) {...});
 *
 * Handlers get the element that carries `data-action` and the event. One
 * listener per event type lives on `document` (click, change, input), so
 * markup swapped in by htmx needs no re-binding. The nearest `data-action`
 * ancestor whose registered event type matches the event runs; an action
 * registered for another event type is skipped, so a `change` action on a
 * <select> does not fire on its click.
 *
 * This replaces inline `on*=` attributes and inline <script> blocks, which a
 * strict script-src would refuse. No later change may add either: new
 * behaviour registers an action here (tests/web/test_frontend_contracts.py
 * fails on an inline handler or script outside its shrinking allowlist).
 *
 * Load this before any script that registers actions.
 */
(function () {
  "use strict";

  var EVENTS = ["click", "change", "input"];
  var handlers = Object.create(null);

  function register(name, eventType, handler) {
    if (typeof name !== "string" || !name) {
      throw new Error("tlmActions.register: name required");
    }
    if (EVENTS.indexOf(eventType) === -1) {
      throw new Error("tlmActions.register: unsupported event " + eventType);
    }
    if (typeof handler !== "function") {
      throw new Error("tlmActions.register: handler required");
    }
    if (handlers[name]) {
      throw new Error("tlmActions.register: duplicate action " + name);
    }
    handlers[name] = { eventType: eventType, handler: handler };
  }

  function dispatch(e) {
    var node = e.target;
    if (!node || typeof node.closest !== "function") return;
    while (node) {
      node = node.closest("[data-action]");
      if (!node) return;
      var entry = handlers[node.getAttribute("data-action")];
      if (entry && entry.eventType === e.type) {
        entry.handler(node, e);
        return;
      }
      node = node.parentElement;
    }
  }

  EVENTS.forEach(function (type) {
    document.addEventListener(type, dispatch);
  });

  window.tlmActions = { register: register };

  /* ── Camera, user, frame and misc actions (WP-70-M2) ───────────────────
   * Replace the inline handlers and scripts of the camera, user and frame
   * templates. Parameters travel in data-* attributes; ids are looked up at
   * click time so markup swapped in by htmx needs no re-binding.
   *
   *   remove-by-id     (click)  remove the element named by data-target.
   *   hide-by-id       (click)  hide the element named by data-target.
   *   close-details    (click)  collapse the enclosing <details>.
   *   submit-form      (change) submit the enclosing form (GET filters).
   *   confirm-click    (click)  confirm() with data-confirm; cancel stops it.
   *   pw-match-check   (click)  block submit when the two passwords differ.
   *   ts-edit/ts-cancel (click) frame drawer capture-timestamp editor.
   *   hostname-manual  (input)  typing a hostname marks its source "manual".
   *   creds-inherit    (change) use-default-credentials checkbox; data-uid.
   *   address-scheme   (input)  show "Basic over HTTP" unless address is https.
   *   apply-detected   (change) fill protocol and URIs from a detected radio.
   *   apply-hostname   (click)  fill the hostname from a query result.
   *   apply-location   (click)  fill latitude and longitude from a query result.
   *   query-accept-all (click)  apply protocol, hostname and location at once.
   *   geo-use-values   (click)  fill coordinates from a polled camera.
   */
  function byId(id) {
    return id ? document.getElementById(id) : null;
  }

  function fieldIn(form, name) {
    return form.querySelector('[name="' + name + '"]');
  }

  register("remove-by-id", "click", function (el) {
    var target = byId(el.getAttribute("data-target"));
    if (target) target.remove();
  });

  register("hide-by-id", "click", function (el) {
    var target = byId(el.getAttribute("data-target"));
    if (target) target.style.display = "none";
  });

  register("close-details", "click", function (el) {
    var details = el.closest("details");
    if (details) details.removeAttribute("open");
  });

  register("submit-form", "change", function (el) {
    var form = el.form;
    if (!form) return;
    if (typeof form.requestSubmit === "function") form.requestSubmit();
    else form.submit();
  });

  register("confirm-click", "click", function (el, event) {
    if (!window.confirm(el.getAttribute("data-confirm") || "Are you sure?")) {
      event.preventDefault();
    }
  });

  register("pw-match-check", "click", function (el, event) {
    var form = el.form;
    if (!form || !form.password || !form.password_confirm) return;
    if (form.password.value === form.password_confirm.value) return;
    event.preventDefault();
    var error = form.querySelector("#new-user-pw-error");
    if (error) error.hidden = false;
    form.password_confirm.focus();
  });

  register("ts-edit", "click", function (el) {
    var row = el.closest("#frame-timestamp-row");
    var form = row && row.querySelector("[data-ts-form]");
    if (!form) return;
    form.hidden = false;
    el.hidden = true;
  });

  register("ts-cancel", "click", function (el) {
    var row = el.closest("#frame-timestamp-row");
    var form = el.closest("[data-ts-form]");
    var edit = row && row.querySelector("[data-ts-edit]");
    if (form) form.hidden = true;
    if (edit) edit.hidden = false;
  });

  register("hostname-manual", "input", function (el) {
    var source = el.form && fieldIn(el.form, "device_hostname_source");
    if (source) source.value = "manual";
  });

  // Hide and disable the per-camera credential inputs while "use default
  // credentials" is ticked (disabled inputs drop out of the submit and out of
  // Query's hx-include, so the server falls back to the default). The markup
  // never hides them itself, so without JS both stay visible and usable.
  function syncCredsInherit(el) {
    var uid = el.getAttribute("data-uid");
    var creds = byId(uid + "-creds");
    var user = byId(uid + "-username");
    var pass = byId(uid + "-password");
    var info = byId(uid + "-default-info");
    if (creds) creds.hidden = el.checked;
    if (user) user.disabled = el.checked;
    if (pass) pass.disabled = el.checked;
    if (info) info.hidden = !el.checked;
  }

  register("creds-inherit", "change", syncCredsInherit);

  // Apply the initial state: on page load and to markup htmx swaps in (the
  // add/edit camera form is a fragment).
  function syncCredsInheritWithin(root) {
    if (!root || typeof root.querySelectorAll !== "function") return;
    var boxes = root.querySelectorAll("[data-action='creds-inherit']");
    for (var i = 0; i < boxes.length; i++) syncCredsInherit(boxes[i]);
    if (root.matches && root.matches("[data-action='creds-inherit']")) {
      syncCredsInherit(root);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      syncCredsInheritWithin(document);
    });
  } else {
    syncCredsInheritWithin(document);
  }
  document.addEventListener("htmx:load", function (e) {
    syncCredsInheritWithin(e.detail && e.detail.elt);
  });

  // Basic over plain HTTP only matters for an http:// or scheme-less address.
  // The checkbox is hidden, never disabled, so its value still submits.
  function syncBasicHttp(addressInput) {
    var group = byId(addressInput.getAttribute("data-uid") + "-allow-basic-group");
    if (!group) return;
    group.hidden = /^\s*https:\/\//i.test(addressInput.value);
  }

  register("address-scheme", "input", syncBasicHttp);

  // When an applied field lives inside the collapsed Advanced disclosure, open
  // it so the change is visible. No-op when there is no <details>.
  function revealAdvanced(input) {
    var details = input && input.closest("details");
    if (details) details.open = true;
  }

  function applyDetected(radio) {
    var form = radio.closest("form");
    if (!form) return;
    var proto = fieldIn(form, "protocol");
    if (proto) proto.value = radio.value;
    var snap = fieldIn(form, "snapshot_uri");
    if (snap) {
      snap.value = radio.getAttribute("data-snapshot-uri") || "";
      revealAdvanced(snap);
    }
    var stream = fieldIn(form, "stream_uri");
    if (stream) stream.value = radio.getAttribute("data-stream-uri") || "";
  }

  function applyHostname(btn) {
    var form = btn.closest("form");
    if (!form) return;
    var value = btn.getAttribute("data-hostname") || "";
    var host = fieldIn(form, "device_hostname");
    if (host) host.value = value;
    var source = fieldIn(form, "device_hostname_source");
    if (source) source.value = "camera";
    // Seed the Name field too, but only when the operator has left it empty.
    var name = fieldIn(form, "name");
    if (name && !name.value) name.value = value;
  }

  // Fill latitude, longitude and geo_source; returns the latitude input.
  function fillCoordinates(btn, form) {
    var lat = fieldIn(form, "latitude");
    if (lat) lat.value = btn.getAttribute("data-lat");
    var lon = fieldIn(form, "longitude");
    if (lon) lon.value = btn.getAttribute("data-lon");
    var source = fieldIn(form, "geo_source");
    if (source) source.value = "camera";
    return lat;
  }

  function applyLocation(btn) {
    var form = btn.closest("form");
    if (!form) return;
    revealAdvanced(fillCoordinates(btn, form));
  }

  register("apply-detected", "change", applyDetected);
  register("apply-hostname", "click", applyHostname);
  register("apply-location", "click", applyLocation);

  register("query-accept-all", "click", function (el) {
    var root = el.closest("#camera-query-results") || document;
    var radio =
      root.querySelector('input[name="detected_protocol"]:checked') ||
      root.querySelector('input[name="detected_protocol"]');
    if (radio) {
      radio.checked = true;
      applyDetected(radio);
    }
    var host = root.querySelector("button[data-hostname]");
    if (host) applyHostname(host);
    var loc = root.querySelector("button[data-lat]");
    if (loc) applyLocation(loc);
  });

  register("geo-use-values", "click", function (el) {
    var form = el.closest("form");
    if (!form) return;
    fillCoordinates(el, form);
    var alert = el.closest(".alert");
    if (alert) alert.remove();
  });

  // The query results applied their recommended protocol as they
  // were parsed. htmx.onLoad runs for the document and each swapped-in element
  // (no eval involved), so the same happens for a fragment loaded by htmx.
  function applyRecommendedProtocol(elt) {
    if (!elt || typeof elt.querySelector !== "function") return;
    var roots = Array.prototype.slice.call(
      elt.querySelectorAll("#camera-query-results")
    );
    var self = elt.closest && elt.closest("#camera-query-results");
    if (self) roots.push(self);
    roots.forEach(function (root) {
      var checked = root.querySelector('input[name="detected_protocol"]:checked');
      if (checked) applyDetected(checked);
    });
  }

  if (window.htmx && typeof window.htmx.onLoad === "function") {
    window.htmx.onLoad(applyRecommendedProtocol);
  }
})();
