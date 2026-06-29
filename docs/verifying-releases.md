# Verifying Releases

Every Timelapse Manager release is accompanied by integrity and provenance
material that lets you confirm you received exactly what was built and signed.
This document describes what is published and how to verify it.

---

## What is published

Each [GitHub release](https://github.com/drcoble/Timelapse_Manager/releases)
includes:

| Asset | Purpose |
|---|---|
| `timelapse-manager-<version>-linux-amd64.tar.gz` | Self-contained bundle tarball |
| `cyclonedx-bom.json` | CycloneDX SBOM (Python deps + bundled FFmpeg) |
| `image-digest.txt` | The exact GHCR image digest pushed during the release |
| `SHA256SUMS` | SHA-256 manifest over the tarball, SBOM, and digest file |
| `SHA256SUMS.asc` | Detached GPG signature over `SHA256SUMS` |
| `cosign.pub` | Public key for verifying the GHCR image signature |
| `KEYS` | Project GPG public key (import before verifying `SHA256SUMS.asc`) |

---

## Verifying the bundle tarball

### 1. Import the project GPG key

```sh
# Download and import the project public key from the release assets.
curl -fsSL https://github.com/drcoble/Timelapse_Manager/releases/latest/download/KEYS \
  | gpg --import
```

Confirm the fingerprint matches the one published on the project's homepage
or GitHub profile before trusting it.

### 2. Verify the GPG signature over the manifest

```sh
gpg --verify SHA256SUMS.asc SHA256SUMS
```

A `Good signature` result means the `SHA256SUMS` file was produced by the
project's signing key and has not been modified.

### 3. Verify each artifact against the manifest

```sh
sha256sum --check SHA256SUMS
```

All three lines should report `OK`. If any file has been tampered with,
`sha256sum` will report a mismatch.

---

## Verifying the Docker image

### 1. Read the signed digest

After verifying `SHA256SUMS` above, read the image digest from the signed
manifest:

```sh
cat image-digest.txt
# e.g. ghcr.io/drcoble/timelapse_manager@sha256:<digest>
```

Because `image-digest.txt` is covered by the verified `SHA256SUMS`, the
digest value itself is authenticated.

### 2. Verify the cosign signature

```sh
# Download the project cosign public key (also a release asset).
curl -fsSLO https://github.com/drcoble/Timelapse_Manager/releases/latest/download/cosign.pub

# Verify the image signature against the key.
cosign verify --key cosign.pub \
  ghcr.io/drcoble/timelapse_manager@sha256:<digest>
```

Install cosign from [sigstore/cosign](https://github.com/sigstore/cosign/releases)
if it is not already present. A successful verification confirms that the image
at that digest was signed by the holder of the project's cosign key.

### 3. Pull by digest (recommended for production)

```sh
docker pull ghcr.io/drcoble/timelapse_manager@sha256:<digest>
```

Pulling by digest instead of by tag guarantees you receive the exact image
that was built, scanned, and signed during the release — not a tag that could
be reassigned.

---

## Verification chain summary

```
KEYS (GPG public key)
  └─ verifies ──▶ SHA256SUMS.asc
                    └─ authenticates ──▶ SHA256SUMS
                                          ├─ covers ──▶ timelapse-manager-*-linux-amd64.tar.gz
                                          ├─ covers ──▶ cyclonedx-bom.json
                                          └─ covers ──▶ image-digest.txt
                                                          └─ names ──▶ GHCR image@sha256:<digest>
                                                                         └─ verified by ──▶ cosign.pub
```

---

## SBOM

`cyclonedx-bom.json` is a [CycloneDX](https://cyclonedx.org/) SBOM listing
all Python dependencies (from the locked `uv.lock`) plus the bundled static
FFmpeg build. Its version, download URL, and SHA-256 are recorded in the SBOM
component for FFmpeg. Feed it to any CycloneDX-compatible tool for licence
analysis, vulnerability scanning, or supply-chain auditing.
