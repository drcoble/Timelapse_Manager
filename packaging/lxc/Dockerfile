# syntax=docker/dockerfile:1
#
# Proxmox CT template rootfs for Timelapse Manager (Debian 13).
#
# Built by build-ct-template.sh, never by hand: the script imports and verifies
# the base, passes the .deb in the build context, exports the final filesystem
# as a tar (no container init layer, so no /.dockerenv) and checks the listing.
#
# BASE_IMAGE is required and must be digest-pinned or a locally imported,
# checksum-verified PVE template (see the script). INSTALL_BASE_PACKAGES=1 is
# only for the plain debian:trixie fallback, which lacks what a CT needs.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}

ARG INSTALL_BASE_PACKAGES=0
ENV DEBIAN_FRONTEND=noninteractive

RUN if [ "${INSTALL_BASE_PACKAGES}" = "1" ]; then \
        apt-get update \
        && apt-get install --no-install-recommends --yes \
            systemd systemd-sysv ifupdown2 iproute2 openssh-server \
            ca-certificates curl tzdata locales procps less \
        && rm -rf /var/lib/apt/lists/*; \
    fi

# Proxmox configures networking through ifupdown (networking.service). The
# Debian base also enables systemd-networkd, which manages no link here, so its
# wait-online unit runs to its 2-minute timeout, fails, and holds up
# network-online.target (and with it timelapse-manager.service) on every boot.
# Mask it so network-online.target comes from ifupdown's own wait-online.
# Disabling alone is not enough: the template ships an empty machine-id, so the
# CT's first boot applies the vendor presets, which re-enable networkd. Presets
# never touch a masked unit. check_rootfs.py fails the build without the masks.
RUN systemctl disable systemd-networkd.service systemd-networkd.socket \
        systemd-networkd-wait-online.service 2>&1 || true \
    && systemctl mask systemd-networkd.service systemd-networkd.socket \
        systemd-networkd-wait-online.service \
    && ! find /etc/systemd/system -name 'systemd-networkd*' -path '*.wants/*' | grep .

# Install the release .deb. Without a booted systemd the postinst only enables
# the unit (the "systemd not booted" guard), which creates the wants/ symlink.
COPY timelapse-manager.deb /tmp/timelapse-manager.deb
RUN apt-get update \
    && apt-get install --no-install-recommends --yes /tmp/timelapse-manager.deb \
    && rm -f /tmp/timelapse-manager.deb

# Loopback-only until configured. Not a package file, so dpkg never touches it.
COPY --chown=root:timelapse --chmod=0640 timelapse-manager.env \
    /etc/timelapse-manager/timelapse-manager.env

# Scrub per-instance state: everything below is regenerated on first boot or by
# `pct create`. /etc/hosts, /etc/hostname and /etc/resolv.conf are container
# bind mounts at build time and are not part of the exported tar; PVE writes
# them for the CT.
RUN rm -rf /var/lib/timelapse-manager/* /var/lib/timelapse-manager/.[!.]* \
    && : > /etc/machine-id \
    && rm -f /var/lib/dbus/machine-id \
    && rm -f /etc/ssh/ssh_host_* \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*
