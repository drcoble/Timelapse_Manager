# Install as a Proxmox CT template

The release publishes a Proxmox container template, `.tar.zst`, built on Debian
13 with the `.deb` pre-installed and the service enabled. Other install
methods: [package](./install-deb-rpm.md), [Docker](./install-docker.md),
[tarball](./installation.md).

## Get the template

The template and its checksum are assets of the
[GitHub Release](https://github.com/drcoble/Timelapse_Manager/releases). Fetch
them on the Proxmox node and put them in template storage:

```sh
ver=<version>
name=debian-13-timelapse-manager_${ver}_amd64.tar.zst
base=https://github.com/drcoble/Timelapse_Manager/releases/download/v$ver
cd /var/lib/vz/template/cache          # the 'local' storage's vztmpl directory
curl -fsSL -O "$base/$name" -O "$base/$name.sha512"
sha512sum --check "$name.sha512"
```

Also compare against `SHA256SUMS` from the same directory
(`sha256sum --check --ignore-missing SHA256SUMS`); see
[Verifying Releases](./verifying-releases.md). Alternatively, download on
another machine and upload through the GUI (Storage, CT Templates, Upload) or
`POST /nodes/<node>/storage/<storage>/upload` with `content=vztmpl`.

## Create the container

```sh
pct create <vmid> local:vztmpl/$name \
  --hostname timelapse \
  --unprivileged 1 \
  --features nesting=1 \
  --memory 2048 --cores 2 \
  --rootfs <storage>:8 \
  --mp0 <storage>:50,mp=/var/lib/timelapse-manager \
  --net0 name=eth0,bridge=vmbr0,ip=dhcp
pct start <vmid>
```

- **Unprivileged is fine, but `nesting=1` is required.** The service unit uses
  systemd sandboxing, which needs mount namespaces; without nesting the start
  fails with `226/NAMESPACE`. (New unprivileged CTs created in the GUI default
  to nesting on.)
- **Sizing:** 2 GiB RAM and 2 cores are a sensible start; more cores speed up
  renders. The root disk needs at least 8 GiB.
- **Data mount point:** put `/var/lib/timelapse-manager` on its own volume
  (`--mp0`), sized for your frames and renders, so the root disk stays small and
  the data survives rebuilding the CT. Size it with the guidance in
  [Installation](./installation.md#tier-sizing-guidance) and
  [Storage](./storage.md). An empty mount point is fine; the service creates its
  files on first start. A new mount point is owned by root, which hides the
  directory the template prepared, so fix the ownership once (see
  [First boot](#first-boot-loopback-only)).
- Put the CT on a bridge that can reach your cameras.

## First boot: loopback only

The template ships `/etc/timelapse-manager/timelapse-manager.env` with
`TLM_SERVER__BIND_ADDRESS=127.0.0.1`. The service therefore listens on loopback
only and the `/first-run` page is **not reachable from the network** until you
choose to open it. Per-instance secrets are not in the template: on first boot
the service migrates an empty database and generates its own `.secret-key`,
local API token and TLS certificate, and Proxmox generates the machine id and SSH
host keys. Two CTs created from one template have different secrets.

1. If you added a data mount point, give the service user the directory, then
   restart the service (once, on a new volume):
   `pct exec <vmid> -- sh -c 'chown timelapse:timelapse /var/lib/timelapse-manager && chmod 0750 /var/lib/timelapse-manager && systemctl restart timelapse-manager'`
2. `pct enter <vmid>`, then check `curl -fsS http://127.0.0.1:8080/healthz`.
3. Create the first administrator without exposing the first-run page:
   `timelapse-manager user create --role admin` (the headless route, which
   bypasses first-run; see [Installation](./installation.md#first-run-setup-code)).
   To use the web form instead, print the code with
   `timelapse-manager setup-code`, open the bind address as in step 4 only on a
   network you trust, and complete [first-run setup](./web-ui.md#first-run-setup).
4. Edit `/etc/timelapse-manager/timelapse-manager.env`, set
   `TLM_SERVER__BIND_ADDRESS=0.0.0.0` (or the CT's address), then
   `systemctl restart timelapse-manager`.

Terminate TLS at a reverse proxy before exposing the service widely; see
[Installation](./installation.md#configuration-and-ports) and
[Configuration](./configuration.md).

## Upgrade

Inside the CT, download the newer `.deb` and install it as in
[Install from a package](./install-deb-rpm.md#upgrade). Data and config are
preserved. Or create a new CT from a newer template and re-attach the data
mount point.

## Back up

Back up the data mount point, `/etc/timelapse-manager`, and above all
`.secret-key` (see [Encryption key integrity](./installation.md#encryption-key-integrity)).
`vzdump` of the CT covers all of it.
