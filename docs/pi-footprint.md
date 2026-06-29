# Raspberry Pi / Minimum-Tier Footprint
**Date:** 2026-06-10
**Status:** draft — hardware soak not yet run

This document records the method for measuring Timelapse Manager's resource
footprint on Raspberry Pi 4 (and equivalent ARM64 / minimum-tier hardware) and
holds the target budgets. Measured results are pending a hardware soak.

---

## Target Hardware Tier

| Attribute | Target |
|---|---|
| Board | Raspberry Pi 4 Model B |
| RAM | 2 GB |
| Storage | microSD or USB SSD |
| Architecture | `linux/arm64` |
| OS | Raspberry Pi OS Lite (64-bit) / Debian Bookworm |

The Docker multi-arch image (`linux/arm64`) and the PyInstaller bundle are the
two deployment paths under test.

---

## Measurement Method

The soak consists of a running Timelapse Manager instance with:
- One active project capturing from a real or emulated camera at a 60-second
  interval.
- One render job triggered after 30 frames have accumulated.
- The notification dispatcher enabled with at least one configured channel.

Metrics are sampled at steady state (after the first 10 minutes of operation)
and at peak (during an active render).

| Metric | Tool | Sample point |
|---|---|---|
| RSS (resident set size) | `ps -o rss=` or `smem` | Steady state + render peak |
| CPU utilisation | `top` / `pidstat` | Steady state + render peak |
| Startup time to first HTTP response | `time curl -k https://localhost:8443/healthz` | Cold start |
| SQLite DB size after 30 frames | `du -b data/timelapse.db` | After 30 captures |
| Frame directory size (30 × JPEG) | `du -sh` | After 30 captures |

---

## Target Budgets

These are the design targets. Measured values will be filled in when the soak
runs.

| Metric | Budget | Measured | Notes |
|---|---|---|---|
| RSS at steady state (idle capture) | < 150 MB | TBD — pending hardware soak | Service + SQLite in-process |
| RSS at render peak (FFmpeg subprocess excluded) | < 300 MB | TBD — pending hardware soak | FFmpeg is a separate process |
| CPU at steady state | < 5% single core | TBD — pending hardware soak | Between captures |
| Startup time (cold, Docker) | < 30 s | TBD — pending hardware soak | Includes Python interpreter startup |
| Startup time (cold, bundle) | < 15 s | TBD — pending hardware soak | PyInstaller bundle |

**Note:** FFmpeg's memory and CPU usage during a render is a function of
resolution and codec, not the application itself. It is excluded from the
application RSS budget but will be reported separately.

---

## Status

The hardware soak has not yet been run. All values in the "Measured" column
above are **TBD**. This document will be updated with real measurements when
a Raspberry Pi 4 soak is completed.

The `linux/arm64` build target is defined in the release workflow and the
Docker multi-arch manifest. Post-V1 validation on physical Pi-class hardware
is a planned activity.
