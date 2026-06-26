# UI / UX Style Guide

This is the canonical reference for building the Timelapse Manager web interface.
It governs the server-rendered **Jinja2 + HTMX** layer (FastAPI backend, no
front-end build step). Follow it so every new screen feels like part of one
coherent instrument rather than a collection of pages.

**The code is the source of truth.** The design tokens, component classes, and
JavaScript controllers described here live under
`src/timelapse_manager/web/static/css/` and `.../static/js/`. When this document
and the stylesheet disagree, the stylesheet wins — fix the document. Every hex
value, token name, and class name below is transcribed from
`static/css/tokens.css` and the component stylesheets.

**The floor is non-negotiable: progressive enhancement first.** Every flow must
work as plain HTML over GET/POST. HTMX, drawers, focus traps, and live regions
are enhancements layered on top — never the only path.

---

## Contents

1. [Design principles](#1-design-principles)
2. [Color](#2-color)
3. [Typography](#3-typography)
4. [Spacing, radius & layout](#4-spacing-radius--layout)
5. [Theming (light / dark)](#5-theming-light--dark)
6. [Components](#6-components)
7. [Iconography](#7-iconography)
8. [Motion & elevation](#8-motion--elevation)
9. [Overlay decision rules](#9-overlay-decision-rules)
10. [Navigation & information architecture](#10-navigation--information-architecture)
11. [Smart defaults & minimal input](#11-smart-defaults--minimal-input)
12. [Continuous scroll & deep navigation](#12-continuous-scroll--deep-navigation)
13. [Forms & async feedback](#13-forms--async-feedback)
14. [Accessibility](#14-accessibility)
15. [RBAC UI adaptation](#15-rbac-ui-adaptation)
16. [Checklist: building a new screen](#16-checklist-building-a-new-screen)

---

## 1. Design principles

The interface has a name and a temperament: **Meridian** — a meridian is both a
line of place and the moment the sun crosses its highest point. The app watches
*one place over time*, and the design reflects that: precise, instrument-like,
data-dense but calm.

**Visual principles**

1. **The instrument metaphor.** The UI is a precision instrument, not a
   marketing surface. Layouts read like a dashboard panel: fixed positions,
   machine-legible values, no decoration that doesn't encode data. When in
   doubt, remove.
2. **Color encodes state, not aesthetics.** Color carries operational meaning —
   green means the capture loop is alive, amber means a human paused it, orange
   means the system intervened. Using a status hue decoratively corrupts the
   signal.
3. **Monospace for machine values.** Every number, timestamp, coordinate, size,
   URI, or identifier renders in Geist Mono. This single rule does most of the
   work of hierarchy: monospace is a value, proportional text is a label.
4. **The frame is the product.** Captured frames are the only visual content
   that is not UI chrome. Thumbnails get no decorative borders or shadows — they
   stand alone; the chrome steps back.
5. **Density without clutter.** The design carries a high information load and
   earns it through consistent rhythm, whitespace as separator (not filler), and
   progressive disclosure of secondary data.
6. **Warm linen / indigo dusk.** Light mode is a warm analogue document —
   parchment, dark ink, amber accent. Dark mode is deep indigo night with golden
   accent. Both are low-fatigue environments for sustained monitoring. Do not mix
   the palettes.
7. **Quiet until something needs attention.** Animation exists in exactly three
   places (the running-status pulse, the toast slide-in, the time-ribbon
   reveal). Everything else is instant. If something moves, it matters.

**Interaction principles**

8. **In-context over navigation.** A full page load is reserved for moving to a
   different *place*. Creating, editing, confirming, inspecting, and scanning all
   happen in-context (drawer, inline expansion, popover). Users should rarely
   lose their scroll position or filters to act on a list.
9. **Minimal input, smart defaults.** Ask for the least possible; infer the
   rest. The first action in a creation flow should move the user *forward*
   (query the device, pick the camera) rather than confront an empty form.
10. **Progressive disclosure.** Show the common path; tuck depth behind a
    disclosure that is pre-populated, not blank.
11. **Surface the next step.** The correct next action is the most prominent
    thing on screen, where the user is already looking. Contextual footers render
    the action valid *right now* — not a wall of disabled buttons.
12. **Honest, server-authoritative state.** The server owns truth — validation,
    permissions, computed results. The client never asserts a state the server
    hasn't confirmed.

---

## 2. Color

All color is delivered as CSS custom properties in `tokens.css`. **Never hardcode
a hex value** in a template, component, or inline style — reference the token.

### 2.1 Palette (light / dark)

| Token | Light | Dark | Use |
|---|---|---|---|
| `--color-bg` | `#f5f4f0` | `#0c0d14` | Page background |
| `--color-surface` | `#ffffff` | `#141620` | Cards, nav, header, drawer |
| `--color-surface-2` | `#f0ede8` | `#1c1f2e` | Input background, row hover, secondary fills |
| `--color-surface-3` | `#e5e0d8` | `#242840` | Deepest inset; pressed states |
| `--color-border` | `#ccc6bc` | `#2a2e4a` | Default borders, dividers |
| `--color-border-2` | `#b0a898` | `#3a3f60` | Stronger borders, input borders, focus emphasis |
| `--color-text` | `#1e1c18` | `#e8e9f0` | Body copy, headings, primary labels |
| `--color-text-muted` | `#5a5248` | `#8a8faa` | Secondary labels, nav at rest, hints |
| `--color-text-dim` | `#6a6258` | `#84899f` | Tertiary: form hints, provenance badges, empty-state prose |
| `--color-text-inverse` | `#ffffff` | `#0c0d14` | Text on dark/colored fills (not accent fills) |
| `--color-on-accent` | `#161310` | `#0c0d14` | Text/icons on accent-filled buttons & badges |

### 2.2 Accent — the critical AA rule

The amber-gold accent ships as **two tokens with different permitted uses**.

| Token | Light | Dark | Permitted use |
|---|---|---|---|
| `--color-accent` | `#a07830` | `#c8a96e` | **Large fills & borders only.** Buttons, active-nav border, badge borders, icon fills, focus rings, logo badge. **Never for text or small icons in light mode.** |
| `--color-accent-text` | `#7a5a20` | `#c8a96e` | **Text & inline links.** The only accent value that passes WCAG AA for text on `--color-surface` in light mode. (In dark mode both tokens are equal — `#c8a96e` already passes.) |
| `--color-accent-dim` | `#f5e8c8` | `#2a2210` | Tinted background fill: active chip backgrounds, auth-badge backgrounds, subtle active states |
| `--color-accent-hover` | `#8a6020` | `#dbbf80` | Hover state on accent interactive elements |

> **Do** use `--color-accent-text` for any link or accent-colored text.
> **Don't** write `color: var(--color-accent)` for text in light mode — it fails
> AA contrast. The base stylesheet still sets a transitional `a { color:
> var(--color-accent) }`; new templates should set link color to
> `--color-accent-text`.

### 2.3 Status colors

Each status has a foreground token and a `-bg` companion. Always pair them; never
mix a status foreground with a different status background.

| Token (+`-bg`) | Light | Dark | Meaning | Glow | Pulse |
|---|---|---|---|---|---|
| `--color-running` | `#2e8b57` / `#e3f3ea` | `#5cc882` / `#0a2018` | Capture loop active | Yes | **Yes** (`.status-dot.running` only) |
| `--color-paused` | `#b8860b` / `#f8efd2` | `#d4a017` / `#211800` | **User-**paused | No | No |
| `--color-stopped` | `#6b7280` / `#eceef2` | `#606478` / `#181a28` | Not running, no error | No | No |
| `--color-error` | `#c0392b` / `#fbe6e3` | `#e05c5c` / `#280d0d` | Failure state | Yes | No |
| `--color-lowdisk` | `#c2570c` / `#fbe8d6` | `#f0783c` / `#221008` | **System-initiated** low-disk pause | Yes | No |

- **Glow vs pulse.** All three glowing states (`running`, `error`, `lowdisk`)
  carry a `box-shadow` on `.status-dot`. Only `running` animates that glow
  (`status-pulse`, 2.4s). Error and lowdisk glow *without* movement — urgency is
  the color, not the motion.
- **`lowdisk` is system-initiated only.** Orange is reserved for a capture the
  *system* paused for insufficient disk. A user-paused capture is always
  `paused` (amber). Never use `lowdisk` for a user-created state.
- **Never rely on color alone.** Every status badge carries a text label.

### 2.4 Semantic, role & specialized tokens

| Token | Light | Dark | Use |
|---|---|---|---|
| `--color-success` / `-warning` / `-danger` | aliases of running / paused / error | same | Generic alerts & form validation (use the operational name for *capture* state) |
| `--color-info` / `--color-info-bg` | `#2a6f9e` / `#e3eef6` | `#38bdf8` / `#0d2233` | Informational alerts/toasts |
| `--color-{warning,error,info,success}-desc` | see `tokens.css` | see `tokens.css` | Secondary description text inside an alert |
| `--color-admin-only` / `-bg` / `-border` | `#7c3aed` / `#ede9fe` / `#7c3aed` | `#b07cf0` / `#1e1630` / `#9966cc` | Admin-only badges, admin nav, archived lifecycle badge |
| `--color-device-{bg,text,border}` | resolve to running green | same | The `[device-reported]` provenance badge |
| `--color-ribbon-{day,night,golden,gap,render}` | semi-transparent washes | same | Time-ribbon SVG layers |

**Do** use `--color-running` for any "things are healthy" signal (LDAP
connected, service up). **Don't** invent new hues — all operational signals live
on the five-color status scale.

---

## 3. Typography

| Font | Variable | Used for |
|---|---|---|
| Geist (sans) | `--font-sans` | All UI text: labels, body, headings, buttons, nav |
| Geist Mono | `--font-mono` | **Every machine value**: timestamps, frame counts, sizes, coordinates, durations, IPs, URIs, chip values |

Both are **self-hosted variable woff2** (`fonts.css`, `font-display: swap`). Do
not import web fonts from a CDN — the app is self-hosted and may be air-gapped.

### 3.1 Scale

The baseline is **14px** (`html { font-size: 14px }`). The scale is deliberately
compressed for density. Use only these tokens — never an off-scale size.

| Token | Size | Use |
|---|---|---|
| `--text-2xl` | 24px | Hero numbers (reserved) |
| `--text-xl` | 20px | Drawer titles (`.drawer-title`) |
| `--text-lg` | 16px | In-content section headings |
| `--text-md` | 14px | **Baseline.** Body, labels, buttons, nav |
| `--text-sm` | 13px | Dense table cells, secondary metadata |
| `--text-xs` | 12px | Badges, chips, tight timestamps |
| `--text-2xs` | 11px | Provenance badges, smallest annotation |

### 3.2 Weight & the mono rule

- **400** body/secondary · **500** labels, buttons, nav, active items · **600**
  page titles, drawer/card titles, section headings · **700** app name, badge
  text. Avoid 700 in body copy.
- **Mono is mandatory for machine values.** Apply `.mono` (or `<code>`) inline;
  use `.form-control.mono` for inputs holding technical values. At a glance, a
  reader should be able to tell a value from a label by typeface alone.
- Prose blocks cap at ~480px / ~60 characters. Data rows take their column width.
  Body line-height ~1.5; badges/chips/buttons tighter. Don't inflate line-height
  in tables.

---

## 4. Spacing, radius & layout

### 4.1 The 4px scale

All spacing is a multiple of 4px via `--sp-1` … `--sp-16`. **Never use raw
pixels.** If a need falls between steps, round up.

| Token | px | Common use |
|---|---|---|
| `--sp-1` | 4 | Icon-to-text in a chip, badge margin |
| `--sp-2` | 8 | Inline gaps (icon + label), small stacks |
| `--sp-3` | 12 | Form hint spacing, drawer-header gap, alert padding |
| `--sp-4` | 16 | Card / drawer-body padding, form-row gaps |
| `--sp-5` | 20 | Header horizontal padding, alert-stack margin |
| `--sp-6` | 24 | Main content padding, page-header margin, section breaks |
| `--sp-8` | 32 | Major section separation |
| `--sp-10`–`--sp-16` | 40–64 | Page-level vertical rhythm, empty-state padding |

### 4.2 Radius

The more modal and dominant a surface, the larger its radius.

| Token | px | Applies to |
|---|---|---|
| `--radius-sm` | 3 | Auth/kind/provenance badges, nav section labels |
| `--radius-md` | 5 | Controls: buttons, inputs, select, tabs, dropdowns |
| `--radius-lg` | 8 | Containers: cards, `.alert`, `.toast`, drawer |
| `--radius-xl` | 12 | Full-page overlay: the camera-discover modal |
| `99px` (pill) | — | Status/role badges, chips |

### 4.3 Layout landmarks

CSS-grid app shell: `header` / `nav` / `main`.

- **Header** 52px (`--header-height`), sticky, `z-index: 100`.
- **Left nav** 200px (`--nav-width`) → 56px icon-only at ≤900px
  (`--nav-width-icon`) → fixed bottom bar at ≤600px (`z-index: 90`).
- **Main** `padding: var(--sp-6)`, `overflow-y: auto`.
- **Grids:** `.project-grid` `minmax(360px, 1fr)`; `.frame-grid`
  `minmax(180px, 1fr)` → `140px` mobile; `.detail-layout` `1fr 320px` → stacks at
  ≤900px.
- **Breakpoints:** 900px (icon-only nav, detail layouts stack) and 600px (mobile;
  nav becomes bottom bar). Media queries live in `responsive.css`, which must
  stay loaded **last**.

---

## 5. Theming (light / dark)

- **Mechanism.** `:root` is light by default. `@media (prefers-color-scheme:
  dark)` provides auto-dark; `[data-theme="light"]` / `[data-theme="dark"]` on
  `<html>` force a choice. A user with no explicit choice follows the system.
- **No flash.** An inline script in `<head>` sets `[data-theme]` **before** the
  stylesheet loads, so the correct theme paints on first frame. Don't move theme
  resolution after first paint.
- **Toggle.** `theme.js` cycles light → dark → system, POSTs to `/account/theme`,
  and mirrors to `localStorage`. The control is a `.segmented` radio group.
- **Parity rule.** Every token has both a light and a dark value. When you add a
  token, define it in **all four** blocks (`:root`, the dark media query,
  `[data-theme="light"]`, `[data-theme="dark"]`). Verify both themes before
  shipping.

---

## 6. Components

Each component has its own file under `static/css/components/`. Interactive
behavior is in event-delegated controllers under `static/js/` that survive HTMX
swaps — no re-binding needed.

### 6.1 Component selection

| Need | Component | Class(es) |
|---|---|---|
| Go to a screen | Nav item | `.nav-item` / `.nav-item.active` |
| The one primary action | Primary button | `.btn.btn-primary` |
| Supporting action | Secondary button | `.btn.btn-secondary` |
| Low-priority action | Ghost button | `.btn.btn-ghost` |
| Destructive action | Danger button → inline-confirm | `.btn.btn-danger` → `.inline-confirm` |
| Dense capture-state mark | Status dot | `.status-dot.{running,paused,stopped,error,lowdisk}` |
| Labeled capture state | Status badge | `.status-badge.{…}` |
| Archived project | Lifecycle badge | `.lifecycle-badge.archived` |
| User auth source | Auth badge | `.auth-badge.{local,ldap}` |
| Render origin | Kind badge | `.kind-badge.{manual,scheduled,archive}` |
| Field provenance | Provenance badge | `.badge-device` / `.badge-manual` / `.badge-env` |
| Persistent in-page notice | Alert | `.alert.{warning,error,info,success}` |
| Transient action feedback | Toast | `.toast.{success,error,info,warning}` |
| Create/edit, keep list visible | Drawer | `.drawer` + `.drawer-backdrop` |
| Camera discovery only | Modal | `.discover-modal` |
| Preset value entry | Suggestion chip | `.chip` (writes to a target input) |
| Multi-select filter | Filter chip | `.chip[aria-pressed]` |
| Related views of one place | Tabs | `.tabs[role=tablist]` + `.tab-item` + `.tab-panel` |
| Compare many objects | Data table | `.data-table` |
| Group one object's fields | Card | `.card` |
| Capture timeline | Time ribbon | `.time-ribbon` |
| Per-row secondary actions | Row-actions popover | `.row-actions-menu` / `.row-actions-popover` |
| Theme / mode choice | Segmented control | `.segmented` |

### 6.2 Buttons

**One `.btn-primary` per view** — it orients the eye to the single most important
action. Two equal actions are both `.btn-secondary`; never two primaries side by
side.

| Variant | When |
|---|---|
| `.btn-primary` | Save, Create, Submit — the default action |
| `.btn-secondary` | Export, Duplicate — supporting actions |
| `.btn-ghost` | Cancel, panel toggles, icon toolbar actions |
| `.btn-danger` | Delete/Wipe — **always paired with `.inline-confirm`** |
| `.btn-success` | Resume, Enable — affirmative system actions |
| `.btn-icon` | 36×36 label-less action (**must** carry `aria-label`) |
| `.btn-sm` / `.btn-xs` | Compact / tightest density |

`.btn-danger` is a semantic signal that data will be destroyed — don't use it for
visual weight alone. `[disabled]` dims to 0.4 and disables pointer events.

### 6.3 Badges & the provenance system

- `.status-dot` (8px) when status must fit a dense row; `.status-badge` (pill,
  labeled) when there's room — it's more accessible. If both appear, they must
  agree.
- **Provenance badges** annotate where a field's current value came from:

| Badge | Meaning | Appearance |
|---|---|---|
| `.badge-device` | From the camera/device | Green (running tokens) |
| `.badge-manual` | Entered/overridden by a user | Neutral |
| `.badge-env` | Set by config/environment, read-only in UI | Dim |

When a user edits a `[device-reported]` field, **flip the badge to
`.badge-manual`** so provenance stays honest.

### 6.4 Alert vs toast

| | Alert (`.alert`) | Toast (`.toast`) |
|---|---|---|
| Position | In page flow | Fixed bottom-right (`.toast-region`, z-500) |
| Persistence | Until dismissed / state changes | Auto-dismiss |
| Use for | Conditions affecting the current view (low disk, LDAP error, preflight) | Acknowledgment of an action just done (saved, render started) |

**Do** use an alert when the message must stay visible while the user works;
**use** a toast for a transient acknowledgment they won't re-read.

### 6.5 Chips: suggestion vs filter

Both use `.chip`; the behaviors are distinct and must not be mixed.

- **Suggestion** (`chips.js`): tapping writes a preset value (and unit) into an
  associated input via `data-chip-target` / `data-chip-unit-target`. Always sits
  next to the field it fills. Use for interval/FPS presets.
- **Filter** (`aria-pressed` toggle): tapping toggles inclusion of a category.
  `.chip-level` variants take their level color. Use for the events log filters.

### 6.6 Card vs table

A `.card` groups fields/metadata about **one** object. A `.data-table` compares
**many** objects of the same type (rows hover to `--color-surface-2`; `th` is
uppercase). Fewer than ~3 comparable objects → a card may read better.

### 6.7 Tabs

Client-side (`tabs.js`, no server round-trip). Contract:

```html
<div class="tabs" role="tablist">
  <button class="tab-item active" data-tab-target="#tab-a" aria-selected="true">A</button>
  <button class="tab-item" data-tab-target="#tab-b" aria-selected="false">B</button>
</div>
<div id="tab-a" class="tab-panel" role="tabpanel">…</div>
<div id="tab-b" class="tab-panel" role="tabpanel" hidden>…</div>
```

The active tab is accent text + an accent bottom-border. The no-JS state shows
all panels in document order (panels are only hidden once JS applies `[hidden]`).

### 6.8 Drawer, modal, inline-confirm, popover

These are the in-context surfaces. The decision rule for *which* is
[§9](#9-overlay-decision-rules); their mechanics:

- **Drawer** (`drawer.js`): right-side, 480px, slides in (200ms), `z-300` over a
  `z-299` backdrop. `role="dialog"`, `aria-modal`, traps focus, marks
  `.app-nav` + `.app-main` `inert`, closes on Esc/backdrop, restores focus to the
  opener. Opener is a real `<a href>` that HTMX-loads a fragment into
  `.drawer-body`. **Single-column forms only** — the width won't hold two
  columns.
- **Modal** (`modal.js`): centered `.discover-modal` (radius-xl). Reserved for
  **Camera Discover**. Don't repurpose it — reach for a drawer instead.
- **Inline-confirm** (`.inline-confirm`): an error-background banner HTMX-swapped
  in to *replace* a destructive trigger; the confirm button is the real form
  submit. This is the standard for delete/wipe — **not** a modal "Are you sure?".
- **Row-actions popover** (`.row-actions-popover`): CSS `:hover`/`:focus-within`,
  no JS, with `.danger` items for destructive entries.

### 6.9 Time ribbon

`.time-ribbon` (20px) with `--detail` (36px) and `--compact` (12px) variants — a
server-rendered SVG capture timeline for a **single project**. It is a
pointer-only position indicator and jump shortcut, marked `aria-hidden`; its
accessible twin is the date-jump form (see [§12](#12-continuous-scroll--deep-navigation)).

---

## 7. Iconography

Icons are an **inline SVG sprite** defined once in the shell and referenced by
`<use>`:

```html
<svg class="icon" aria-hidden="true"><use href="#icon-camera"></use></svg>
```

- `.icon` sizes to `1em × 1em` and inherits `currentColor`. Control icon color by
  setting `color` on the parent — **never** set `fill`/`stroke` at the call site.
  Don't hardcode a pixel size unless overriding for a branded mark (e.g. the
  header logo tile).
- **Accessibility.** Decorative icons paired with a visible label get
  `aria-hidden="true"`. An icon that is the *only* label of a control needs an
  `aria-label` (or a `.visually-hidden` span) on the control.
- **No external icon libraries** (Heroicons/FontAwesome/Phosphor) and no
  CDN/data-URI icon fonts. The motif is geometric and instrument-like —
  consistent stroke width, no gradients. Review the existing sprite before adding
  a symbol to avoid synonyms.
- A few HTML entities survive as micro-indicators in tight legacy contexts; that
  is acceptable where it exists, but **don't introduce new Unicode dingbats or
  emoji as icons** — use the sprite.

---

## 8. Motion & elevation

### 8.1 Transitions

Two tokens cover all animation. Don't use `transition: all` in new components —
enumerate properties (`background`, `color`, `border-color`, `transform`,
`opacity`).

| Token | Value | Use |
|---|---|---|
| `--transition-fast` | `120ms ease` | Hover, focus ring, chip/nav state, badge color |
| `--transition-mid` | `200ms ease` | Surface appearance: drawer slide, backdrop fade, toast slide-in |

### 8.2 Z-index ladder

Use only these levels — never an arbitrary value. A new sticky element slots into
50–60 and must not overlap the header.

| Value | Layer |
|---|---|
| 50–55 | Sticky scrubber / position panels |
| 60 | Frames selection / action bar |
| 90 | Mobile bottom nav (≤600px) |
| 100 | Header (sticky) |
| 200 | Hover panels (user menu, alerts panel) |
| 299 | Drawer / modal backdrop |
| 300 | Drawer / modal |
| 500 | Toasts |

### 8.3 The signature animation & reduced motion

The one purposeful entrance is the **time-ribbon reveal** — a left-to-right
clip-path wipe (~400ms) on first render. Don't replicate it elsewhere.

**Every animation must respect `@media (prefers-reduced-motion: reduce)`** —
`status-pulse`, `toast-in`, drawer/backdrop transitions, and the ribbon
cursor/reveal all suppress under it. Rule: purely aesthetic motion → `none`;
structural motion (a panel opening) → keep an instant fallback.

### 8.4 Elevation

Overlaid surfaces show depth with `box-shadow`, not background contrast — drawers
and hover dropdowns only. **Don't** shadow in-flow surfaces (cards, tables,
inputs).

---

## 9. Overlay decision rules

**This is the most consequential interaction decision on a new screen.** Pick
from the table; don't improvise.

> **Navigate only when the destination is a place. Everything else stays
> in-context** — drawer, inline expansion, inline-confirm, or popover. **Never
> build a wizard with page-to-page lock-in.**

| Mechanism | Form | Use when… | Example |
|---|---|---|---|
| **Full navigation** | New page | The target is a *place* the user dwells in | Opening a project's detail page |
| **Drawer** | Right slide-in, focus-trapped | A create/detail flow that benefits from keeping the originating list visible | New Project; Frame detail |
| **Modal** | Centered, focus-trapped | A self-contained action that must own the screen briefly (rare — justify it) | Camera Discover scan |
| **Inline expansion** | In-page, no overlay | Editing/adding inside a table where neighboring rows are useful reference | Add / Edit Camera |
| **Inline-confirm** | Error-bg banner replacing the trigger | A destructive confirmation; confirm = real form submit | Delete project/camera/render |
| **Row-actions popover** | CSS hover/focus menu | A per-row menu of secondary actions | "⋯" Archive/Delete on a row |
| **Contextual popover** | Anchored popover | A lifecycle action attached to one object's detail | Archive/Delete on project detail |

**Choose in order:** (1) target is a place → navigate; (2) destructive confirm →
inline-confirm; (3) create/detail keeping the list visible → drawer; (4)
edit/add inside a table → inline expansion; (5) per-row/object secondary actions
→ popover; (6) bounded action that must own the screen → modal (only if nothing
above fits).

**Don't** use a modal for confirmation, creation, or editing. **Don't** chain
overlays into a page-locked wizard — reveal dependent steps inline in the same
surface. **Don't** stack overlays — one layer at a time.

---

## 10. Navigation & information architecture

The nav is **flat**: Dashboard, Cameras, Projects, Frames, Renders, Events,
About, then an admin-only divider with Users and Settings. No nested sub-section
labels, no expandable trees.

| Mechanism | Use when… | Example |
|---|---|---|
| **Nav item** | The destination is a distinct top-level place | Cameras, Projects, Renders |
| **Tab in a page** | Related views of one place the user switches among without leaving | Settings → System / Network / LDAP / Notifications / Credentials; Events → Operations / Audit |
| **In-page section** | Content belonging to the current view, best seen together | A project's render settings; a camera's advanced URIs |

**Do** group cohesive sub-areas as tabs in one page (the Settings/Events
precedent). **Don't** promote a sub-area to its own nav item just because it has a
lot of content — if it shares the same place, it's a tab.

**Header.** Logo + breadcrumb + theme toggle + alerts panel + role badge + user
menu. The **breadcrumb is the primary "where am I"** signal (the nav is flat);
set it on every screen, and on detail views show the parent → child path. The
**alerts panel** polls `/alerts/summary` every 30s with a badge count, opening on
hover/focus. The **role badge** shows admin/viewer; **operator is hidden** because
it is the default role — a badge for the normal case is noise.

---

## 11. Smart defaults & minimal input

Require the minimum, infer the rest, and make the first action move the user
forward. These patterns apply to any new create/configure form.

- **Query-first golden path.** When form content can be *discovered*, make
  discovery the first and most prominent action. On Add Camera, Address + "Query
  camera" comes first; the device response populates protocol, hostname,
  geolocation, URIs, and profiles, and one **"Accept all"** button fills the
  form. *Rule:* detected-then-confirmed beats typed-from-scratch — lead with the
  detect action, let one button accept, leave every field editable after.
- **Provenance chips.** Any pre-filled value shows its origin —
  `[device-reported]` (green), `[manual]`, or `[env]` — and the chip updates the
  instant a user overrides it. Never silently pre-fill.
- **Progressive disclosure.** Advanced fields (snapshot/stream URI, PTZ, lat/lon)
  live under a `▸ Advanced` disclosure, **pre-populated and collapsed**. The
  default surface is everything a novice needs; depth is filled-in, not blank.
- **Conditional reveal.** Hide a field set until a non-default choice makes it
  relevant — e.g. "Use global default" credentials is pre-checked; per-camera
  fields appear only when unchecked.
- **Suggestion chips with consequence labels.** Offer common-good values as
  chips. When a value's meaning is non-obvious, label the chip with its
  real-world consequence (e.g. *"At 5m interval, 30fps = 2.5h real-time per 1s
  video"*), not just the number.
- **Dependent-field HTMX loading.** When one selection determines another's
  options, fetch the dependents on `change` and auto-select the obvious default
  (selecting a camera loads its stream profiles + PTZ presets).
- **Inline pre-flight & compatibility.** Compute consequences server-side and
  swap them in *as soon as the inputs exist* — storage pre-flight (MB/day vs free
  disk, green/amber/red), render combo-check (incompatible encoder/container
  shows a warning in place of the streamable chip). Feedback panels swap in
  place; they never navigate.
- **Empty-state in place.** If a flow needs a prerequisite the user lacks, let
  them create it inside the flow ("Add a camera instead" inline-expands the camera
  form inside the New Project drawer) and offer to seed config from a prior
  instance ("Copy settings from project Y").

---

## 12. Continuous scroll & deep navigation

Frames and Events share **one keyset-scroll spine**. Reuse it for any
chronological, append-only list that can grow without bound. Do **not** use it for
small, bounded, or rank-ordered lists — those are ordinary rendered tables.

**The spine**

- **Newest at top, append older on scroll.**
- A **real `<a>` sentinel** with `hx-trigger="revealed"` fires `GET
  …/batch?before=<cursor>` and swaps **itself** (`outerHTML`) for the next batch
  plus a new sentinel. An empty response yields a **stable end-cap** ("Beginning
  of campaign / log — DATE").
- **Keyset cursor only, never offset.** Frames key on per-project
  `sequence_index`; Events on the global monotonic `id`. Offset paging drifts and
  double-counts as the list grows; keyset is stable under inserts and
  soft-deletes.
- **`content-visibility: auto`** on tiles/rows caps paint cost as the DOM grows
  (no virtualization, no extra JS).

**One-directional + deep nav**

- **No bidirectional scroll.** To move toward *newer* content, use the date-jump
  form or the ribbon — not upward scrolling.
- **Date/time jump form** — always present, keyboard-first, and the **no-JS and
  accessible deep-navigation equivalent** (submits GET). This is the canonical way
  to land anywhere in the timeline.
- **Time ribbon** (single-project frames only) — pointer-only jump + scroll
  indicator, `aria-hidden`, an enhancement whose accessible twin is the date-jump
  form. Never make it the only way to reach a position.

**Live-tail pill** (`role="status"`, sticky top): *"↑ N new since you started —
Refresh."* Appears **only on the 0→N transition**, never auto-inserts at the top,
and does **not** re-announce on every poll. Clicking resets to newest.

**Filter state rides the sentinel.** Changing project / level chips / search
resets the list to newest, and all filter state is threaded onto the sentinel's
`before=` URL so deep-loaded batches stay scoped (and the no-JS path stays
correct). When you add a filter, thread it through the sentinel.

---

## 13. Forms & async feedback

- **Server-side validation is authoritative.** Server-validated forms use
  `novalidate` so the browser doesn't pre-empt or contradict the server; HTMX
  swaps the server's error fragment inline. The user never navigates to see an
  error.
- **Inline result panels swap in place.** Query, Validate, pre-flight, and
  combo-check render where the user is looking — never a new page or modal.
- **Save / Cancel pairs.** Every editable surface offers both; **Cancel returns
  the prior context** (closes the drawer, collapses the inline form) with no side
  effects.
- **Destructive = inline-confirm, not a modal.** See [§9](#9-overlay-decision-rules).

**Don't** block submission on client-side HTML5 validation when the server owns
the rules — it produces inconsistent messages and breaks the no-JS path.

---

## 14. Accessibility

Requirements, not enhancements — build them in from the first screen.

- **Focus.** Drawer and modal trap focus, close on **Esc**, mark the background
  `inert`, and restore focus to the opener on close. Focus ring is `2px solid
  var(--color-accent)` with `1px` offset via `:focus-visible` on every
  interactive element.
- **Live regions — announce once.** Async outcomes (selection count,
  batch-loaded announcements, live-tail) use `role="status"` polite regions.
  Announce concisely and **once** — don't re-announce on every poll or appended
  batch. Chatty live regions are worse than silent ones.
- **Keyboard order.** The date-jump form is reachable **before** the grid, so
  keyboard users deep-navigate without traversing the whole list. Grid tiles are
  focusable (Enter opens) with roving-tabindex arrow navigation.
- **No-JS floor.** Sentinels are real links; jump/filter forms submit GET;
  drawers fall back to full pages; tabs show all panels. Verify every new flow
  with JavaScript disabled.
- **aria-hidden with a twin.** Any decorative or pointer-only visual (the time
  ribbon) is `aria-hidden` and **must have an accessible twin** that does the
  same job for keyboard/SR users (the date-jump form). Never ship a pointer-only
  control without its twin.
- **Color is never the only signal** (see [§2.3](#23-status-colors)); decorative
  icons are `aria-hidden`; reduced motion is respected
  ([§8.3](#83-the-signature-animation--reduced-motion)).

---

## 15. RBAC UI adaptation

Roles are **admin / operator / viewer**. The UI adapts by **omission, not
concealment**.

- **Absent, not hidden.** Privileged controls are **not rendered into the DOM**
  for unauthorized roles — gate them server-side
  (`{% if current_user.role == 'admin' %}`). Never render-then-CSS-hide; a
  present-but-hidden control is a discoverability and security leak.
- **Server enforcement is the source of truth.** Routes authorize independently
  (403 / redirect). UI gating is a usability layer over an already-secure
  backend, never the only line of defense.
- **Admin-only surfaces:** the admin nav items, the Events → Audit tab, and Users
  / Settings.
- **Valid-action-only footers.** Contextual footers render **only the action
  valid for the current state and role** — not a row of disabled buttons. A
  disabled button is clutter and a question ("why can't I?"); an absent one keeps
  the next step unambiguous.

---

## 16. Checklist: building a new screen

Before opening a PR for a new screen or component, confirm:

- [ ] **Tokens, not literals.** No raw hex, px, font, or radius values — every
      visual value references a token from `tokens.css`.
- [ ] **Accent text uses `--color-accent-text`** (light-mode AA);
      `--color-accent` is fills/borders only.
- [ ] **Machine values are mono** (`.mono` / `.form-control.mono`); status uses a
      status token *and* a text label.
- [ ] **Overlay chosen from [§9](#9-overlay-decision-rules)** — navigation only
      for a place; destructive confirms are inline-confirm, not modals.
- [ ] **Sub-areas are tabs**, not new nav items, when they share a place.
- [ ] **Smart defaults applied** — detect/pre-fill where safe, provenance chips
      on pre-filled fields, advanced fields under a pre-populated disclosure.
- [ ] **Unbounded chronological lists use the keyset spine** (real-link sentinel,
      end-cap, date-jump twin, filter state on the sentinel).
- [ ] **Server owns validation** (`novalidate` + inline HTMX error swap); Save /
      Cancel both present; Cancel restores context.
- [ ] **No-JS floor verified** — links/forms work with JS disabled.
- [ ] **Accessibility** — focus trap + restore on overlays, `role="status"`
      announces once, keyboard order sane, pointer-only visuals `aria-hidden`
      with a twin.
- [ ] **Reduced-motion override** added for any new animation/transition.
- [ ] **RBAC by omission** — privileged markup gated server-side and the route
      enforces it; footers show only the valid next action.
- [ ] **Both themes checked** — any new token defined in all four theme blocks.
