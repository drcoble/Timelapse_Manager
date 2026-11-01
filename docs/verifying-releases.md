# Verifying Releases

Every Timelapse Manager release is accompanied by integrity and provenance
material that lets you confirm you received exactly what was built. This
document describes what is published and how to verify it.

---

## Where releases are published

Each release is a [GitHub Release](https://github.com/drcoble/Timelapse_Manager/releases)
of `drcoble/Timelapse_Manager`. Its assets download from:

```
https://github.com/drcoble/Timelapse_Manager/releases/download/v<version>/<file>
```

The Docker image is published to `ghcr.io/drcoble/timelapse_manager`. No account
or token is needed to download any of it. `SHA256SUMS` is uploaded **last**, so
its presence means the upload is complete.

| Asset | Purpose |
|---|---|
| `timelapse-manager-<version>-linux-amd64.tar.gz` | Self-contained bundle tarball |
| `timelapse-manager_<version>-1_amd64.deb` | Debian package |
| `timelapse-manager-<version>-1.x86_64.rpm` | RPM package (signed) |
| `timelapse-manager-rpm-signing-key.asc` | Public key for the RPM signature, fingerprint `C0CA17A09723DA4E17C71D77A50138DBA443C270` |
| `debian-13-timelapse-manager_<version>_amd64.tar.zst` and `.sha512` | Proxmox CT template and its checksum |
| `cyclonedx-bom.json` | CycloneDX SBOM (Python deps + bundled FFmpeg + vendored assets) |
| `sbom-image.cdx.json` | SBOM of the Docker image (OS packages and Python packages) |
| `sbom-lxc.cdx.json` | SBOM of the CT template |
| `image-digest.txt` | The digest of the published Docker image |
| `SHA256SUMS` | SHA-256 manifest over every other file in the release, including the `.sha512` file and the signing key |

`<version>` is the PEP 440 release version (`0.2.0`). The package files use the
mapped package version, which differs from it only for pre-releases (`0.2.0~rc6`,
see [package versions](./install-deb-rpm.md#package-versions-and-ordering)). The
image is identified by its digest, which is listed inside the checksummed
`image-digest.txt`.

## Verifying the files

Download `SHA256SUMS` and the files you intend to use, then check them:

```sh
ver=0.2.0
base=https://github.com/drcoble/Timelapse_Manager/releases/download/v$ver
curl -fsSLO "$base/SHA256SUMS"
curl -fsSLO "$base/<file>"                                 # each file you need
sha256sum --check --ignore-missing SHA256SUMS
```

Every line for a file you downloaded should report `OK`. This detects a
corrupted or incomplete download. `SHA256SUMS` is served from the same place as
the files, so on its own it does not prove who published them; the RPM
signature and a pull of the image by digest, below, are the stronger checks. The
CT template also has a `.sha512` file for `sha512sum --check`.

### The RPM signature

The `.rpm` carries its own signature, made with the RPM signing key. Download
`timelapse-manager-rpm-signing-key.asc` from the same release, confirm its
fingerprint, import it and check the package:

```sh
gpg --show-keys --with-fingerprint timelapse-manager-rpm-signing-key.asc
sudo rpm --import timelapse-manager-rpm-signing-key.asc
rpm -K timelapse-manager-<version>-1.x86_64.rpm
```

The fingerprint must be `C0CA17A09723DA4E17C71D77A50138DBA443C270`. See
[Install from a package](./install-deb-rpm.md#enterprise-linux-dnf). The `.deb`
is not separately signed; rely on `SHA256SUMS` for it.

### The Docker image

`image-digest.txt` is covered by `SHA256SUMS`. After checking it, pull the image
by its digest, so you run the build that was published and not a tag that could
be moved:

```sh
curl -fsSLO "$base/image-digest.txt"
cat image-digest.txt
docker pull ghcr.io/drcoble/timelapse_manager@sha256:<digest>
```

Use the `sha256:` value from `image-digest.txt`. See
[Install with Docker](./install-docker.md#pull-by-digest).

## Verification chain summary

```
SHA256SUMS
  ├─ covers ──▶ the tarball, .deb, .rpm, CT template, signing key, SBOMs
  └─ covers ──▶ image-digest.txt
                  └─ names ──▶ ghcr.io/drcoble/timelapse_manager@sha256:<digest>

timelapse-manager-rpm-signing-key.asc ──▶ verifies ──▶ the .rpm signature (rpm -K)
```

---

## SBOM

`cyclonedx-bom.json` is a [CycloneDX](https://cyclonedx.org/) SBOM listing
all Python dependencies (from the locked `uv.lock`) plus the bundled static
FFmpeg build. Its version, download URL, and SHA-256 are recorded in the SBOM
component for FFmpeg. Feed it to any CycloneDX-compatible tool for licence
analysis, vulnerability scanning, or supply-chain auditing.

The SBOM also lists the bundled htmx and Geist fonts, which
`web/static/vendor.json` records with their versions and SHA-256 values. The
release build runs `uv sync --locked`, so update `uv.lock` (`uv lock`) in the
same commit as any dependency change.
