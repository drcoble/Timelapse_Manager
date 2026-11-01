# Install from a .deb or .rpm package

Release packages install the same self-contained bundle as the tarball
(Python runtime, dependencies and the pinned FFmpeg), plus the systemd unit,
the service user and the managed directories. They are attached to each
[GitHub Release](https://github.com/drcoble/Timelapse_Manager/releases) of
`drcoble/Timelapse_Manager`; no account or token is needed to download them.

**There is no apt or dnf repository.** You download the package from the
Release and install the file. Upgrading means downloading the newer package
and installing it over the old one (see [Upgrade](#upgrade)); `apt upgrade`
and `dnf upgrade` do not find new releases by themselves.

Other install methods: [tarball and `install.sh`](./installation.md),
[Docker](./install-docker.md), [Proxmox CT template](./install-proxmox-ct.md).

## Requirements

- **linux/amd64** with systemd.
- **.deb:** Debian 13 (`trixie`) is the tested target. Other current Debian and
  Ubuntu releases can install the same file with `apt install ./file.deb`.
- **.rpm:** EL9 (Rocky, Alma, RHEL 9) or newer. The bundle is built in an
  Enterprise Linux 9 container and the release fails if any file in it needs a
  glibc newer than 2.34, so EL9 is the minimum.

## Package versions and ordering

Package versions are derived from the release's PEP 440 version so that a
pre-release always sorts below its final release:

| Release version | Package version |
|---|---|
| `0.2.0.dev10` | `0.2.0~dev10` |
| `0.2.0rc6` | `0.2.0~rc6` |
| `0.2.0` | `0.2.0` |
| `0.2.0.post1` | `0.2.0+post1` |

`~` sorts before everything in both dpkg and rpm, so `0.2.0~dev10` <
`0.2.0~rc6` < `0.2.0` < `0.2.0+post1`, and installing the final release over a
pre-release is an upgrade. Only `devN` and `rcN` pre-releases are supported
(alpha and beta tags are refused by the version mapping). The package release
number is always `1`.

In the examples below, `<ver>` is the release version (`0.2.0`) and the package
files are named `timelapse-manager_<ver>-1_amd64.deb` and
`timelapse-manager-<ver>-1.x86_64.rpm`. Release assets live at
`https://github.com/drcoble/Timelapse_Manager/releases/download/v<ver>/<file>`.

## Download and verify

Fetch the package and the checksum list from the Release, and check them before
installing:

```sh
ver=0.2.0
base=https://github.com/drcoble/Timelapse_Manager/releases/download/v$ver
curl -fsSLO "$base/SHA256SUMS"
curl -fsSLO "$base/timelapse-manager_${ver}-1_amd64.deb"      # Debian / Ubuntu
curl -fsSLO "$base/timelapse-manager-${ver}-1.x86_64.rpm"     # EL
sha256sum --check --ignore-missing SHA256SUMS
```

See [Verifying Releases](./verifying-releases.md) for the full chain, including
the RPM signature.

## Debian and Ubuntu (apt)

Run as root, in the directory where you downloaded the file:

```sh
apt install ./timelapse-manager_0.2.0-1_amd64.deb
```

Using `./` makes apt read the local file and resolve its dependencies from your
configured repositories.

The install creates the `timelapse` user and group, creates
`/var/lib/timelapse-manager` and `/etc/timelapse-manager`, migrates the database
as the service user, enables the unit and starts it, then waits up to 15 seconds
for `/healthz` to answer. See
[Upgrade restart check](./installation.md#upgrade-restart-check-packages).

## Enterprise Linux (dnf)

The RPMs are signed with the project's RPM signing key, fingerprint
`C0CA17A09723DA4E17C71D77A50138DBA443C270`. The public key is attached to
each Release as `timelapse-manager-rpm-signing-key.asc`. The same key signs every
release, so any release's copy works.

Download it, compare the fingerprint, and import it once:

```sh
ver=0.2.0
base=https://github.com/drcoble/Timelapse_Manager/releases/download/v$ver
curl -fsSLO "$base/timelapse-manager-rpm-signing-key.asc"
gpg --show-keys --with-fingerprint timelapse-manager-rpm-signing-key.asc   # compare with C0CA17A0...
sudo rpm --import timelapse-manager-rpm-signing-key.asc
rpm -K timelapse-manager-${ver}-1.x86_64.rpm       # should report the signature as OK
```

Then install the file:

```sh
sudo dnf install ./timelapse-manager-0.2.0-1.x86_64.rpm
```

Whichever way you get the key, confirm that the fingerprint gpg or dnf shows is
`C0CA17A09723DA4E17C71D77A50138DBA443C270` before accepting it.

## After installing

`timelapse-manager` is on `PATH` (`/usr/bin/timelapse-manager`). Get the one-time
setup code for the first-run page:

```sh
sudo timelapse-manager setup-code
```

The service listens on HTTP 8080 and HTTPS 8443 by default. Open
`https://<host>:8443/` (self-signed certificate) and finish
[first-run setup](./web-ui.md#first-run-setup). Configuration lives in
`/etc/timelapse-manager/config.yml` and the optional
`/etc/timelapse-manager/timelapse-manager.env`; see
[Configuration](./configuration.md) and
[Installation: configuration and ports](./installation.md#configuration-and-ports).
Data (database, frames, renders, exports, key and token) lives under
`/var/lib/timelapse-manager`.

## Upgrade

There is no repository to poll, so an upgrade is a new download. Fetch and
verify the newer package as in [Download and verify](#download-and-verify), then
install it over the old one:

```sh
sudo apt install ./timelapse-manager_<new-ver>-1_amd64.deb      # Debian / Ubuntu
sudo dnf install ./timelapse-manager-<new-ver>-1.x86_64.rpm     # EL
```

The upgrade keeps `config.yml`, the env file, the database, frames and
`.secret-key`. The post-install script migrates the database (writing a
pre-migration snapshot first), restarts the service and reports its health. A
failed migration leaves the service as it was, with instructions on stderr. Read
[Upgrading to 0.2.0](./installation.md#upgrading-to-020) before moving from an
earlier release.

## Remove and purge

- `apt remove timelapse-manager` or `dnf remove timelapse-manager` stops and
  disables the service and deletes the program files. **Data is left in place:**
  `/var/lib/timelapse-manager`, `/etc/timelapse-manager` and the `timelapse`
  user. Reinstalling picks it up again.
- `apt purge` behaves the same: the package never deletes operator data.
  Delete `/var/lib/timelapse-manager` and `/etc/timelapse-manager` by hand (and
  `userdel timelapse`) if you want a clean slate. Back up `.secret-key` first if
  you might restore a database later.

## Moving from a tarball install

An install made with `install.sh` writes its unit to
`/etc/systemd/system/timelapse-manager.service`. That file **takes precedence
over** the package's unit in `/usr/lib/systemd/system`, so after installing the
package the old unit (with the old paths and settings) would still be the one
that runs. Both installs use the same user and directories, so data carries
over. To move:

```sh
sudo systemctl stop timelapse-manager
sudo systemctl disable timelapse-manager
sudo rm /etc/systemd/system/timelapse-manager.service
sudo systemctl daemon-reload
sudo apt install ./timelapse-manager_<ver>-1_amd64.deb   # or: dnf install ./timelapse-manager-<ver>-1.x86_64.rpm
```

Back up `/var/lib/timelapse-manager` and `/etc/timelapse-manager` first. Check
`systemctl cat timelapse-manager` shows the unit under `/usr/lib/systemd/system`.
A tarball install does not have to move to a package; both are supported.
