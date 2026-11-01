# Timelapse Manager -- developer task runner.
#
# Thin wrappers over `uv` (the environment and dependency manager). Run
# `make help` for a summary of the available targets.

# Dev TLS material (git-ignored). Generated on demand by `make run`.
DEV_CERT := .dev-cert.pem
DEV_KEY  := .dev-cert-key.pem

# Local development ports.
HTTPS_PORT := 8443

.DEFAULT_GOAL := help

.PHONY: help bootstrap dev-cert run test lint typecheck fmt migrate \
        mock-cameras check bundle release packages ct-template sbom sign image

help: ## Show this help.
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "} {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

bootstrap: ## Provision the toolchain and install dependencies from the lockfile.
	uv sync

# File target: the dev cert is only (re)generated when missing, so `run` does
# not regenerate it on every invocation. A single invocation of the generator
# writes both the cert and the key, so depending on the cert alone is enough
# (and avoids running the recipe twice on Make versions without grouped
# targets).
$(DEV_CERT):
	uv run python dev/gen_dev_cert.py

dev-cert: $(DEV_CERT) ## Generate the self-signed dev TLS cert (if missing).

run: $(DEV_CERT) ## Run the app locally over HTTPS (generates the dev cert if missing).
	uv run uvicorn timelapse_manager.app:create_app --factory \
		--host 0.0.0.0 --port $(HTTPS_PORT) \
		--ssl-certfile $(DEV_CERT) --ssl-keyfile $(DEV_KEY)

test: ## Run the test suite (parallel across all cores via pytest-xdist).
	uv run pytest -n auto

lint: ## Lint the source and tests.
	uv run ruff check src tests

typecheck: ## Type-check the source.
	uv run mypy src

fmt: ## Format the source and tests.
	uv run ruff format src tests

fmt-check: ## Verify formatting without modifying files (CI parity).
	uv run ruff format --check src tests

migrate: ## Apply database migrations.
	uv run alembic upgrade head

mock-cameras: ## Start the mock RTSP + HTTP-snapshot cameras for adapter dev.
	uv run python dev/mock_cameras/run.py

check: lint fmt-check typecheck test ## Run lint, format check, typecheck, and tests (pre-push gate).

# --- Packaging / release ----------------------------------------------------
# The released linux/amd64 artifacts are built by the release CI on a Linux
# runner. These targets drive the same scripts locally; on a non-Linux host the
# bundle validates the PyInstaller spec only (it is not the shipped artifact).

bundle: ## Freeze the app into a relocatable bundle (no tarball/smoke).
	./packaging/release.sh --skip-smoke

release: ## Build the bundle + tarball and run a local smoke test.
	./packaging/release.sh

# The SBOM lists what ships, so it scans a runtime-only environment
# (.venv-runtime, no dev tooling) with the dev environment's cyclonedx-py. The
# sync gets its own environment variable, kept off the `uv run` that follows.
sbom: ## Generate the CycloneDX SBOM (dist/cyclonedx-bom.json) incl. bundled FFmpeg.
	mkdir -p dist
	UV_PROJECT_ENVIRONMENT=.venv-runtime uv sync --locked --no-dev
	uv run python dev/gen_sbom.py --venv .venv-runtime --output dist/cyclonedx-bom.json

sign: release sbom ## Build artifacts, then write SHA256SUMS + a detached signature.
	# All artifacts must share one directory so the manifest uses bare names;
	# release writes the tarball to dist/ and sbom writes the SBOM there too.
	./dev/sign_release.sh \
		dist/timelapse-manager-*.tar.gz \
		dist/cyclonedx-bom.json

# Build the .deb and .rpm from the bundle `make release` froze (needs nfpm on
# PATH; a signed .rpm also needs TLM_RPM_SIGNING_KEY_FILE, see the script).
packages: ## Build the .deb and .rpm from the frozen bundle (run `make release` first).
	@version="$$(uv run python -c 'import timelapse_manager as t; print(t.__version__)')"; \
	./packaging/build-linux-packages.sh "$$version" amd64

# Build the Proxmox CT template from the .deb in dist/ (needs Docker with
# buildx, zstd and the .deb from `make packages`).
ct-template: ## Build the Proxmox CT template (.tar.zst) from the .deb in dist/.
	@version="$$(uv run python -c 'import timelapse_manager as t; print(t.__version__)')"; \
	deb="$$(ls dist/timelapse-manager_*_amd64.deb 2>/dev/null | head -n 1)"; \
	[ -n "$$deb" ] || { echo "error: no .deb in dist/ (run make packages)" >&2; exit 1; }; \
	./packaging/lxc/build-ct-template.sh --deb "$$deb" --version "$$version" --out-dir dist

# Build the linux/amd64 Docker image locally. The Dockerfile carries real
# digest-pinned defaults for its three bases (python, debian, uv), so a hand
# build is as reproducible as release CI. Override one with, for example,
# `make image PYTHON_IMAGE=python:3.12-slim-trixie@sha256:...`. The pinned FFmpeg
# is fetched from the public URL in ffmpeg-pin.json and sha256-checked. To try a
# private mirror first, set TLM_FFMPEG_MIRROR_URL and pass GITEA_TOKEN in the
# environment (buildx secret). Requires Docker with buildx.
IMAGE_NAME ?= ghcr.io/drcoble/timelapse_manager
comma := ,
image: ## Build the linux/amd64 Docker image (digest-pinned bases) locally.
	@version="$$(uv run python -c 'import timelapse_manager as t; print(t.__version__)')"; \
	revision="$$(git rev-parse HEAD 2>/dev/null || echo unknown)"; \
	created="$$(date -u +%Y-%m-%dT%H:%M:%SZ)"; \
	echo "==> $(IMAGE_NAME):$$version"; \
	docker buildx build --platform linux/amd64 \
		$(if $(PYTHON_IMAGE),--build-arg PYTHON_IMAGE=$(PYTHON_IMAGE)) \
		$(if $(FFMPEG_BASE_IMAGE),--build-arg FFMPEG_BASE_IMAGE=$(FFMPEG_BASE_IMAGE)) \
		$(if $(UV_IMAGE),--build-arg UV_IMAGE=$(UV_IMAGE)) \
		--build-arg VERSION=$$version --build-arg VCS_REF=$$revision \
		--build-arg BUILD_DATE=$$created \
		$(if $(TLM_FFMPEG_MIRROR_URL),--build-arg TLM_FFMPEG_MIRROR_URL=$(TLM_FFMPEG_MIRROR_URL)) \
		$(if $(GITEA_TOKEN),--secret id=gitea_token$(comma)env=GITEA_TOKEN) \
		-f docker/Dockerfile \
		-t $(IMAGE_NAME):$$version --load .
