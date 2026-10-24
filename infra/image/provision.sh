#!/usr/bin/env bash
# Root or Route agent image provisioner.
#
# Turns a stock Ubuntu 24.04 machine (amd64 or arm64) into the agent's desktop.
# The same script runs everywhere: Lima, Multipass, a cloud VM, a Packer build,
# or any Ubuntu box over SSH. It is idempotent; re-running it converges.
#
# Usage (as root, from the unpacked bundle directory):
#   ./provision.sh
#
# Environment:
#   RR_HOME_DEVICE   Block device for the persistent /home/agent disk. Formatted
#                    only if it has no filesystem. If unset, a disk labelled
#                    rr-home is used when present, else the root disk.
#   RR_EGRESS_MODE   open (default): full internet except private/link-local
#                    networks, every connection logged. allowlist: only hosts in
#                    files/egress/allowlist.txt, through a filtering proxy.
#   RR_ALLOW_DOMAINS Comma-separated extra hosts for allowlist mode.
#   RR_WITH_BROWSER  1 (default) to install headless Chromium via Playwright.
#   RR_EXTRA_PACKAGES Comma-separated apt packages on top of packages.txt.
#   RR_BUNDLE_REV    Source revision recorded in the image manifest.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FILES="$HERE/files"
# shellcheck source-path=SCRIPTDIR source=manifest.env
source "$HERE/manifest.env"

AGENT_USER=agent
EXEC_USER=rr-exec
WORK_GROUP=rrwork
AGENT_HOME=/home/agent
EXEC_HOME=/var/lib/rr-exec
RR_ETC=/etc/rootorroute
RR_OPT=/opt/rootorroute
EGRESS_MODE="${RR_EGRESS_MODE:-open}"
WITH_BROWSER="${RR_WITH_BROWSER:-1}"

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

have_systemd() { [[ -d /run/systemd/system ]]; }

# ── Preflight ────────────────────────────────────────────────────────────────

[[ $EUID -eq 0 ]] || die "run as root"
[[ $EGRESS_MODE == open || $EGRESS_MODE == allowlist ]] ||
  die "RR_EGRESS_MODE must be open or allowlist, got $EGRESS_MODE"

# shellcheck source=/dev/null
source /etc/os-release
if [[ "${ID:-}" != ubuntu || "${VERSION_ID:-}" != "$UBUNTU_RELEASE" ]]; then
  warn "expected Ubuntu $UBUNTU_RELEASE, found ${PRETTY_NAME:-unknown}; continuing"
fi

case "$(dpkg --print-architecture)" in
  amd64) ARCH=amd64 ;;
  arm64) ARCH=arm64 ;;
  *) die "unsupported architecture: $(dpkg --print-architecture)" ;;
esac

if ! have_systemd; then
  warn "systemd is not running (container?); services will be installed but not started"
fi

export DEBIAN_FRONTEND=noninteractive

# Download a file and verify its sha256, or fail.
fetch() {
  local url=$1 sha=$2 dest=$3
  curl -fsSL --retry 3 -o "$dest" "$url"
  echo "$sha  $dest" | sha256sum -c --quiet - || die "checksum mismatch for $url"
}

# Pick the per-architecture value of a manifest variable, e.g. sha_for NODE.
sha_for() {
  local var="${1}_SHA256_${ARCH^^}"
  printf '%s' "${!var}"
}

# ── Packages ─────────────────────────────────────────────────────────────────

log "Installing OS packages"
mapfile -t packages < <(sed -e 's/#.*//' -e 's/[[:space:]]//g' -e '/^$/d' "$HERE/packages.txt")
if [[ -n "${RR_EXTRA_PACKAGES:-}" ]]; then
  IFS=',' read -ra extra_packages <<<"$RR_EXTRA_PACKAGES"
  for pkg in "${extra_packages[@]}"; do
    pkg="$(echo "$pkg" | xargs)"
    [[ "$pkg" =~ ^[a-z0-9][a-z0-9.+-]*$ ]] || die "invalid package name in RR_EXTRA_PACKAGES: $pkg"
    packages+=("$pkg")
  done
fi
apt-get update -qq
apt-get install -y -qq --no-install-recommends "${packages[@]}" >/dev/null
# Ubuntu ships these under other names; expose the usual ones.
ln -sf /usr/bin/fdfind /usr/local/bin/fd
ln -sf /usr/bin/batcat /usr/local/bin/bat

log "Installing Docker Engine"
install -d -m 0755 /etc/apt/keyrings
if [[ ! -f /etc/apt/keyrings/docker.asc ]]; then
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
fi
echo "deb [arch=$ARCH signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
  docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin \
  docker-ce-rootless-extras >/dev/null

# ── Toolchains (pinned, checksum-verified) ───────────────────────────────────

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

if [[ "$(/usr/local/bin/node --version 2>/dev/null || true)" != "v$NODE_VERSION" ]]; then
  log "Installing Node $NODE_VERSION"
  node_arch=$([[ $ARCH == amd64 ]] && echo x64 || echo arm64)
  fetch "https://nodejs.org/dist/v$NODE_VERSION/node-v$NODE_VERSION-linux-$node_arch.tar.xz" \
    "$(sha_for NODE)" "$TMP/node.tar.xz"
  rm -rf /usr/local/lib/nodejs
  install -d /usr/local/lib/nodejs
  tar -xJf "$TMP/node.tar.xz" -C /usr/local/lib/nodejs --strip-components=1
  for bin in node npm npx corepack; do
    ln -sf "/usr/local/lib/nodejs/bin/$bin" "/usr/local/bin/$bin"
  done
fi
# pnpm and yarn shims; each version is fetched on first use per project.
/usr/local/bin/corepack enable --install-directory /usr/local/bin

if [[ "$(/usr/local/go/bin/go version 2>/dev/null | awk '{print $3}')" != "go$GO_VERSION" ]]; then
  log "Installing Go $GO_VERSION"
  fetch "https://go.dev/dl/go$GO_VERSION.linux-$ARCH.tar.gz" "$(sha_for GO)" "$TMP/go.tar.gz"
  rm -rf /usr/local/go
  tar -xzf "$TMP/go.tar.gz" -C /usr/local
fi
# On the default PATH (and sudo's secure_path) for every user.
ln -sf /usr/local/go/bin/go /usr/local/bin/go
ln -sf /usr/local/go/bin/gofmt /usr/local/bin/gofmt

if [[ "$(/usr/local/bin/uv --version 2>/dev/null | awk '{print $2}')" != "$UV_VERSION" ]]; then
  log "Installing uv $UV_VERSION"
  uv_arch=$([[ $ARCH == amd64 ]] && echo x86_64 || echo aarch64)
  fetch "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-$uv_arch-unknown-linux-gnu.tar.gz" \
    "$(sha_for UV)" "$TMP/uv.tar.gz"
  tar -xzf "$TMP/uv.tar.gz" -C "$TMP"
  install -m 0755 "$TMP/uv-$uv_arch-unknown-linux-gnu/uv" "$TMP/uv-$uv_arch-unknown-linux-gnu/uvx" /usr/local/bin/
fi

if [[ "$(/usr/local/bin/gh --version 2>/dev/null | head -1 | awk '{print $3}')" != "$GH_VERSION" ]]; then
  log "Installing gh $GH_VERSION"
  fetch "https://github.com/cli/cli/releases/download/v$GH_VERSION/gh_${GH_VERSION}_linux_$ARCH.tar.gz" \
    "$(sha_for GH)" "$TMP/gh.tar.gz"
  tar -xzf "$TMP/gh.tar.gz" -C "$TMP"
  install -m 0755 "$TMP/gh_${GH_VERSION}_linux_$ARCH/bin/gh" /usr/local/bin/gh
fi

# Rust: the toolchain is shared and read-only under /opt/rust; each user's
# registry cache and `cargo install` output go to their own ~/.cargo.
RUST_HOME=/opt/rust
if [[ "$(RUSTUP_HOME=$RUST_HOME/rustup "$RUST_HOME/cargo/bin/rustc" --version 2>/dev/null | awk '{print $2}')" != "$RUST_VERSION" ]]; then
  log "Installing Rust $RUST_VERSION"
  rust_arch=$([[ $ARCH == amd64 ]] && echo x86_64 || echo aarch64)
  fetch "https://static.rust-lang.org/rustup/archive/$RUSTUP_VERSION/$rust_arch-unknown-linux-gnu/rustup-init" \
    "$(sha_for RUSTUP)" "$TMP/rustup-init"
  chmod +x "$TMP/rustup-init"
  RUSTUP_HOME="$RUST_HOME/rustup" CARGO_HOME="$RUST_HOME/cargo" "$TMP/rustup-init" -y -q \
    --no-modify-path --profile minimal --default-toolchain "$RUST_VERSION" \
    --component rustfmt --component clippy >/dev/null
  chmod -R a+rX "$RUST_HOME"
fi
for bin in rustup rustc rustdoc cargo rustfmt cargo-fmt cargo-clippy clippy-driver; do
  ln -sf "$RUST_HOME/cargo/bin/$bin" "/usr/local/bin/$bin"
done

if [[ "$(/usr/local/bin/bun --version 2>/dev/null || true)" != "$BUN_VERSION" ]]; then
  log "Installing Bun $BUN_VERSION"
  bun_arch=$([[ $ARCH == amd64 ]] && echo x64 || echo aarch64)
  fetch "https://github.com/oven-sh/bun/releases/download/bun-v$BUN_VERSION/bun-linux-$bun_arch.zip" \
    "$(sha_for BUN)" "$TMP/bun.zip"
  unzip -q -o "$TMP/bun.zip" -d "$TMP"
  install -m 0755 "$TMP/bun-linux-$bun_arch/bun" /usr/local/bin/bun
  ln -sf /usr/local/bin/bun /usr/local/bin/bunx
fi

if [[ "$(/usr/local/bin/deno --version 2>/dev/null | head -1 | awk '{print $2}')" != "$DENO_VERSION" ]]; then
  log "Installing Deno $DENO_VERSION"
  deno_arch=$([[ $ARCH == amd64 ]] && echo x86_64 || echo aarch64)
  fetch "https://github.com/denoland/deno/releases/download/v$DENO_VERSION/deno-$deno_arch-unknown-linux-gnu.zip" \
    "$(sha_for DENO)" "$TMP/deno.zip"
  unzip -q -o "$TMP/deno.zip" -d "$TMP"
  install -m 0755 "$TMP/deno" /usr/local/bin/deno
fi

# Toolchain settings for every session, including `sudo -u rr-exec` (pam_env).
sed -i '/# rootorroute-begin/,/# rootorroute-end/d' /etc/environment
cat "$FILES/etc/environment" >> /etc/environment

if [[ $WITH_BROWSER == 1 ]]; then
  marker="$RR_OPT/browsers/.playwright-$PLAYWRIGHT_VERSION"
  if [[ ! -f $marker ]]; then
    log "Installing headless Chromium (Playwright $PLAYWRIGHT_VERSION)"
    install -d -m 0755 "$RR_OPT" "$RR_OPT/browsers"
    PLAYWRIGHT_BROWSERS_PATH="$RR_OPT/browsers" \
      npx --yes "playwright@$PLAYWRIGHT_VERSION" install --with-deps chromium >/dev/null
    chmod -R a+rX "$RR_OPT/browsers"
    touch "$marker"
  fi
  install -m 0644 "$FILES/etc/apparmor-browsers" /etc/apparmor.d/opt.rootorroute.browsers
fi

# ── Persistent home disk ─────────────────────────────────────────────────────
# Mounted before the agent user exists so the home directory lands on it.

home_dev=""
if [[ -n "${RR_HOME_DEVICE:-}" ]]; then
  [[ -b "$RR_HOME_DEVICE" ]] || die "RR_HOME_DEVICE=$RR_HOME_DEVICE is not a block device"
  if [[ -z "$(blkid -o value -s TYPE "$RR_HOME_DEVICE" 2>/dev/null || true)" ]]; then
    log "Formatting $RR_HOME_DEVICE as ext4 (label rr-home)"
    mkfs.ext4 -q -L rr-home "$RR_HOME_DEVICE"
  fi
  home_dev="$RR_HOME_DEVICE"
elif blkid -L rr-home >/dev/null 2>&1; then
  home_dev="$(blkid -L rr-home)"
fi

if [[ -n "$home_dev" ]]; then
  uuid="$(blkid -o value -s UUID "$home_dev")"
  if ! grep -q "UUID=$uuid" /etc/fstab; then
    log "Adding persistent home disk $home_dev to /etc/fstab"
    echo "UUID=$uuid $AGENT_HOME ext4 defaults,nofail 0 2" >> /etc/fstab
  fi
  install -d "$AGENT_HOME"
  mountpoint -q "$AGENT_HOME" || mount "$AGENT_HOME"
else
  warn "no persistent home disk; /home/agent lives on the boot disk (use 'rr vm backup' before rebuilding)"
fi

# ── Users ────────────────────────────────────────────────────────────────────

log "Creating users"
getent group "$WORK_GROUP" >/dev/null || groupadd --system "$WORK_GROUP"

if ! id "$AGENT_USER" >/dev/null 2>&1; then
  useradd --create-home --home-dir "$AGENT_HOME" --shell /bin/bash \
    --user-group --groups "$WORK_GROUP" "$AGENT_USER"
fi
# The agent never gets sudo, even if the base image put the first user in it.
gpasswd -d "$AGENT_USER" sudo >/dev/null 2>&1 || true

if ! id "$EXEC_USER" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "$EXEC_HOME" --shell /usr/sbin/nologin \
    --user-group --groups "$WORK_GROUP" "$EXEC_USER"
fi

# Rootless Docker needs subordinate id ranges.
grep -q "^$AGENT_USER:" /etc/subuid || usermod --add-subuids 200000-265535 "$AGENT_USER"
grep -q "^$AGENT_USER:" /etc/subgid || usermod --add-subgids 200000-265535 "$AGENT_USER"

AGENT_UID=$(id -u "$AGENT_USER")
EXEC_UID=$(id -u "$EXEC_USER")

# ── Home layout ──────────────────────────────────────────────────────────────
# rr-exec can traverse /home/agent and work in worktrees/, but cannot read
# state, memory, logs, or secrets-adjacent files.

log "Laying out $AGENT_HOME"
chown "$AGENT_USER:$AGENT_USER" "$AGENT_HOME"
chmod 0711 "$AGENT_HOME"
for d in state memory cache logs; do
  install -d -o "$AGENT_USER" -g "$AGENT_USER" -m 0700 "$AGENT_HOME/$d"
done
# Worktrees live in repos/<repo>.git/worktrees too (index, HEAD), so the sandbox
# user needs group write on both. Pushing still needs a token only rr-tools has.
for d in repos worktrees; do
  install -d -o "$AGENT_USER" -g "$WORK_GROUP" -m 2770 "$AGENT_HOME/$d"
  setfacl -d -m "g:$WORK_GROUP:rwX" "$AGENT_HOME/$d"
done

# ── Egress ───────────────────────────────────────────────────────────────────

log "Configuring egress ($EGRESS_MODE)"
install -d -m 0755 "$RR_ETC" "$RR_ETC/egress"
echo "$EGRESS_MODE" > "$RR_ETC/egress/mode"

sed -e "s/@AGENT_UID@/$AGENT_UID/g" -e "s/@EXEC_UID@/$EXEC_UID/g" \
  "$FILES/egress/egress-$EGRESS_MODE.nft.tmpl" > "$RR_ETC/egress/egress.nft"
chmod 0644 "$RR_ETC/egress/egress.nft"

install -m 0644 "$FILES/etc/agent.env" "$RR_ETC/agent.env"
install -m 0644 "$FILES/etc/profile.sh" /etc/profile.d/rootorroute.sh

if [[ $EGRESS_MODE == allowlist ]]; then
  {
    grep -vE '^\s*(#|$)' "$FILES/egress/allowlist.txt"
    if [[ -n "${RR_ALLOW_DOMAINS:-}" ]]; then
      IFS=',' read -ra extra <<<"$RR_ALLOW_DOMAINS"
      for host in "${extra[@]}"; do
        host="$(echo "$host" | xargs)"
        [[ -n "$host" ]] || continue
        [[ "$host" =~ ^[A-Za-z0-9.-]+$ ]] || die "invalid domain in RR_ALLOW_DOMAINS: $host"
        printf '^%s$\n' "${host//./\\.}"
      done
    fi
  } | sort -u > "$RR_ETC/egress/allowlist.compiled"
  install -m 0644 "$FILES/egress/tinyproxy.conf" /etc/tinyproxy/tinyproxy.conf
  cat "$FILES/etc/proxy.env" >> "$RR_ETC/agent.env"
elif [[ -n "${RR_ALLOW_DOMAINS:-}" ]]; then
  warn "RR_ALLOW_DOMAINS is ignored in open egress mode"
fi

# ── Scoped sudo: agent → rr-exec only ────────────────────────────────────────

install -m 0440 "$FILES/etc/sudoers-rr-exec" /etc/sudoers.d/rootorroute
visudo -cq -f /etc/sudoers.d/rootorroute || die "invalid sudoers drop-in"

# ── Agent runtime Python ─────────────────────────────────────────────────────

log "Installing Python $PYTHON_VERSION for the agent runtime"
install -d -m 0755 "$RR_OPT"
UV_PYTHON_INSTALL_DIR="$RR_OPT/python" /usr/local/bin/uv python install "$PYTHON_VERSION" --quiet
if [[ ! -x "$RR_OPT/venv/bin/python" ]]; then
  UV_PYTHON_INSTALL_DIR="$RR_OPT/python" /usr/local/bin/uv venv --quiet --python "$PYTHON_VERSION" "$RR_OPT/venv"
fi

# ── systemd units ────────────────────────────────────────────────────────────

log "Installing systemd units"
for unit in rr-egress.service rr-tools.service rr-supervisor.service; do
  install -m 0644 "$FILES/systemd/$unit" "/etc/systemd/system/$unit"
done

# ── Rootless Docker (needs a running systemd) ────────────────────────────────

install -m 0644 "$FILES/etc/apparmor-rootlesskit" /etc/apparmor.d/usr.bin.rootlesskit

if have_systemd; then
  systemctl daemon-reload

  log "Starting egress firewall"
  systemctl enable --now rr-egress.service
  systemctl restart rr-egress.service
  if [[ $EGRESS_MODE == allowlist ]]; then
    systemctl enable tinyproxy.service
    systemctl restart tinyproxy.service
  else
    systemctl disable --now tinyproxy.service >/dev/null 2>&1 || true
  fi

  log "Setting up rootless Docker for $AGENT_USER"
  # Loads the rootlesskit and browser profiles.
  systemctl reload apparmor.service || systemctl restart apparmor.service
  # The agent uses rootless Docker only; the root daemon stays off.
  systemctl disable --now docker.service docker.socket >/dev/null 2>&1 || true
  loginctl enable-linger "$AGENT_USER"
  systemctl start "user@$AGENT_UID.service"

  install -d -o "$AGENT_USER" -g "$AGENT_USER" \
    "$AGENT_HOME/.config" "$AGENT_HOME/.config/systemd" \
    "$AGENT_HOME/.config/systemd/user" "$AGENT_HOME/.config/systemd/user/docker.service.d"
  docker_proxy="$AGENT_HOME/.config/systemd/user/docker.service.d/proxy.conf"
  if [[ $EGRESS_MODE == allowlist ]]; then
    install -m 0644 -o "$AGENT_USER" -g "$AGENT_USER" "$FILES/etc/docker-proxy.conf" "$docker_proxy"
  else
    rm -f "$docker_proxy"
  fi

  as_agent() {
    runuser -u "$AGENT_USER" -- env \
      XDG_RUNTIME_DIR="/run/user/$AGENT_UID" \
      DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$AGENT_UID/bus" \
      HOME="$AGENT_HOME" "$@"
  }
  if [[ ! -f "$AGENT_HOME/.config/systemd/user/docker.service" ]]; then
    as_agent dockerd-rootless-setuptool.sh install >/dev/null
  fi
  as_agent systemctl --user daemon-reload
  as_agent systemctl --user enable docker.service >/dev/null 2>&1
  as_agent systemctl --user restart docker.service

  # Inert until the agent package is installed (ConditionPathExists).
  systemctl enable rr-tools.service rr-supervisor.service >/dev/null 2>&1
fi

if ! grep -q DOCKER_HOST "$AGENT_HOME/.profile" 2>/dev/null; then
  echo "export DOCKER_HOST=unix:///run/user/$AGENT_UID/docker.sock" >> "$AGENT_HOME/.profile"
  chown "$AGENT_USER:$AGENT_USER" "$AGENT_HOME/.profile"
fi
grep -q DOCKER_HOST "$RR_ETC/agent.env" ||
  echo "DOCKER_HOST=unix:///run/user/$AGENT_UID/docker.sock" >> "$RR_ETC/agent.env"

# ── Image manifest ───────────────────────────────────────────────────────────

log "Writing image manifest"
jq -n \
  --arg rev "${RR_BUNDLE_REV:-unknown}" \
  --arg built "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg os "${PRETTY_NAME:-unknown}" \
  --arg kernel "$(uname -r)" \
  --arg arch "$ARCH" \
  --arg node "$NODE_VERSION" --arg go "$GO_VERSION" --arg uv "$UV_VERSION" \
  --arg gh "$GH_VERSION" --arg python "$PYTHON_VERSION" \
  --arg docker "$(dpkg-query -W -f='${Version}' docker-ce 2>/dev/null || true)" \
  --arg git "$(git --version | awk '{print $3}')" \
  --arg rust "$RUST_VERSION" --arg bun "$BUN_VERSION" --arg deno "$DENO_VERSION" \
  --arg java "$(java -version 2>&1 | head -1 | awk -F'"' '{print $2}')" \
  --arg dotnet "$(dotnet --version 2>/dev/null || echo none)" \
  --arg ruby "$(ruby -e 'print RUBY_VERSION' 2>/dev/null || echo none)" \
  --arg php "$(php -r 'echo PHP_VERSION;' 2>/dev/null || echo none)" \
  --arg clang "$(clang -dumpversion 2>/dev/null || echo none)" \
  --arg browser "$([[ $WITH_BROWSER == 1 ]] && echo "playwright-$PLAYWRIGHT_VERSION" || echo none)" \
  --arg egress "$EGRESS_MODE" \
  --argjson home_disk "$([[ -n "$home_dev" ]] && echo true || echo false)" \
  '{bundle_rev: $rev, built_at: $built, os: $os, kernel: $kernel, arch: $arch,
    home_disk: $home_disk, egress: $egress,
    versions: {node: $node, go: $go, uv: $uv, gh: $gh, python: $python,
               rust: $rust, bun: $bun, deno: $deno, java: $java, dotnet: $dotnet,
               ruby: $ruby, php: $php, clang: $clang,
               docker: $docker, git: $git, browser: $browser}}' \
  > "$RR_ETC/image-manifest.json"

log "Provisioning complete"
cat "$RR_ETC/image-manifest.json"
