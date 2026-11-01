# Install with Docker

Each release publishes a `linux/amd64` image to the GitHub Container Registry:

```
ghcr.io/drcoble/timelapse_manager:<version>
```

The image is public; no login is needed to pull it.

It is built on Debian 13 (`python:3.12-slim-trixie`), carries the pinned FFmpeg
(the build named in `ffmpeg-pin.json`), runs as a non-root user and serves plain
HTTP on port 8080. Other install methods: [package](./install-deb-rpm.md),
[Proxmox CT](./install-proxmox-ct.md), [tarball](./installation.md).

## Pull by digest

Each release publishes its image digest in `image-digest.txt`, an asset of the
[GitHub Release](https://github.com/drcoble/Timelapse_Manager/releases). The
image tag is the release version, for example `0.2.0`, with no leading `v`;
`latest` follows final releases only. `image-digest.txt` is covered by
`SHA256SUMS` (see [Verifying Releases](./verifying-releases.md)). Pull by that
digest rather than by tag; a tag can be moved, a digest cannot:

```sh
ver=0.2.0
base=https://github.com/drcoble/Timelapse_Manager/releases/download/v$ver
curl -fsSLO "$base/SHA256SUMS"
curl -fsSLO "$base/image-digest.txt"
sha256sum --check --ignore-missing SHA256SUMS
cat image-digest.txt                  # carries the sha256:<digest>
IMAGE=ghcr.io/drcoble/timelapse_manager@sha256:<digest>
docker pull "$IMAGE"
```

The image carries OCI labels (`org.opencontainers.image.version`, `.revision`,
`.created`, `.source`); `docker inspect --format '{{json .Config.Labels}}'` shows
the release and commit it was built from.

## Run it

```sh
docker run -d --name timelapse \
  -p 127.0.0.1:8080:8080 \
  -v tlm-data:/data -v tlm-config:/config \
  --restart unless-stopped \
  "$IMAGE"
```

| Item | Value |
|---|---|
| Port | **8080**, plain HTTP. No HTTPS listener and the HTTP-to-HTTPS redirect is off (`TLM_SERVER__REDIRECT_HTTP_TO_HTTPS=false`). |
| User | `app`, **uid/gid 10001**, not root. |
| Volume `/data` | Database (`timelapse.db`), captured frames (`/data/frames/<project>/`), **renders and frame exports** (`/data/frames/renders/`), `.secret-key`, local API token, generated certificate, `setup-code`. |
| Volume `/config` | Optional `config.yml` (point at it with `TLM_CONFIG=/config/config.yml`) and key files you supply. Never bake secrets into the image. |
| Health check | `curl --fail http://localhost:8080/healthz` every 30 s (5 s timeout, 3 retries, 10 s start period); `docker ps` shows `healthy`. |
| FFmpeg | `/opt/ffmpeg/bin/ffmpeg`, selected through `TLM_RENDER__FFMPEG_BINARY`. |

Renders and exports sit beside the frames, in the `renders` directory next to a
project's frame directory (`render.output_subdir`), so a project with its own
`storage_path` keeps them under that path's parent. Mount that location as well
if you use `storage_path`. See [Storage](./storage.md).

### Bind mounts and ownership

The process runs as uid 10001. With named volumes Docker sets this up for you.
With a **bind mount**, the host directory must be writable by uid 10001 or the
service cannot create its database:

```sh
sudo mkdir -p /srv/tlm/data /srv/tlm/config
sudo chown -R 10001:10001 /srv/tlm/data /srv/tlm/config
docker run -d --name timelapse -p 127.0.0.1:8080:8080 \
  -v /srv/tlm/data:/data -v /srv/tlm/config:/config "$IMAGE"
```

### Startup, migrations and secrets

The entrypoint runs `timelapse migrate` (idempotent) as the `app` user and then
starts uvicorn as PID 1, so an upgrade migrates the database on first start of
the new image, with the usual pre-migration snapshot. The `.secret-key` lives in
`/data`; **keep it with the database**. A container recreated with the same
volumes keeps its key, data and administrator; one started against an empty
`/data` and a restored database without the original key refuses to start
(exit status 78, see
[Encryption key integrity](./installation.md#encryption-key-integrity)).

### First-run setup

```sh
docker exec timelapse timelapse setup-code
```

Open `http://<host>:8080/first-run`, enter the code and create the administrator
(see [first-run setup](./web-ui.md#first-run-setup)). `docker exec timelapse
timelapse user create --role admin` is the headless alternative. The container's
CLI is `timelapse`; on a package install it is `timelapse-manager`.

Put TLS in front with a reverse proxy that forwards `X-Forwarded-Proto: https`
(see [Installation: ports](./installation.md#ports-docker)), and list the proxy in
`server.trusted_proxies` ([Configuration](./configuration.md)).

## Compose

`docker/docker-compose.yml` in the repository builds the image from source. To
run the published image, use a file like this and pin the digest:

```yaml
services:
  app:
    image: ghcr.io/drcoble/timelapse_manager@sha256:<digest>
    ports:
      - "127.0.0.1:8080:8080"
    volumes:
      - tlm-data:/data
      - tlm-config:/config
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "--fail", "--silent", "http://localhost:8080/healthz"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 10s

volumes:
  tlm-data:
  tlm-config:
```

```sh
docker compose up -d
docker compose exec app timelapse setup-code
```

## Upgrade

Pull the new digest, change the `image:` (or `docker run`) reference and
recreate. The volumes keep everything; the entrypoint migrates.

```sh
docker compose pull && docker compose up -d
```

Read [Upgrading to 0.2.0](./installation.md#upgrading-to-020) first when moving
between minor releases. Before upgrading, check that no render is running
(`GET /api/v1/system/render-activity`), because stopping the container ends it.

## Building the image yourself

`make image` builds the image locally, tagged `<IMAGE_NAME>:<version>` (set
`make image IMAGE_NAME=<name>` to choose the name).
The three base images (Python, the FFmpeg download stage, uv) are pinned by digest
in `docker/Dockerfile`; refreshing them is described in
[FFmpeg refresh](./ffmpeg-refresh.md#6-run-the-cve-scan).
