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
        mock-cameras check bundle release sbom sign image

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

sbom: ## Generate the CycloneDX SBOM (dist/cyclonedx-bom.json) incl. bundled FFmpeg.
	mkdir -p dist
	uv run python dev/gen_sbom.py --output dist/cyclonedx-bom.json

sign: release sbom ## Build artifacts, then write SHA256SUMS + a detached signature.
	# All artifacts must share one directory so the manifest uses bare names;
	# release writes the tarball to dist/ and sbom writes the SBOM there too.
	./dev/sign_release.sh \
		dist/timelapse-manager-*.tar.gz \
		dist/cyclonedx-bom.json

# Build the linux/amd64 Docker image locally, resolving the base-image digest
# the same way release CI does so a hand-built image is digest-pinned too. The
# Dockerfile's PYTHON_IMAGE default is a placeholder that fails on purpose; the
# real digest is supplied here via --build-arg. Requires Docker with buildx.
image: ## Build the linux/amd64 Docker image (digest-pinned base) locally.
	@digest="$$(docker buildx imagetools inspect python:3.12-slim-bookworm \
		--format '{{.Manifest.Digest}}')"; \
	version="$$(uv run python -c 'import timelapse_manager as t; print(t.__version__)')"; \
	echo "==> base python:3.12-slim-bookworm@$$digest"; \
	docker buildx build --platform linux/amd64 \
		--build-arg PYTHON_IMAGE=python:3.12-slim-bookworm@$$digest \
		-f docker/Dockerfile \
		-t timelapse-manager:$$version --load .
