#!/usr/bin/env bash
# Prepare a fresh Ubuntu 24.04 box to run the GlassBox stack.
#
#   sudo bash deploy/bootstrap.sh
#
# Idempotent: safe to re-run. Installs Docker, opens only the three ports the
# stack needs, creates a non-root service user, and adds swap — the 2 GB
# instances this runs on have enough memory to *run* the stack but not always
# enough to *build* the image on one vCPU, and an OOM-killed build leaves a
# half-made image that fails in confusing ways later.
set -euo pipefail

SERVICE_USER="${SERVICE_USER:-glassbox}"
SWAP_GB="${SWAP_GB:-2}"

log() { printf '\n== %s\n' "$*"; }

if [[ $EUID -ne 0 ]]; then
    echo "run as root: sudo bash deploy/bootstrap.sh" >&2
    exit 1
fi

log "swap (${SWAP_GB}G)"
if [[ -f /swapfile ]]; then
    echo "  /swapfile already present"
else
    fallocate -l "${SWAP_GB}G" /swapfile
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >>/etc/fstab
    echo "  created and enabled"
fi
# Prefer RAM; swap is here to survive the build, not to run the stack from disk.
sysctl -qw vm.swappiness=10
grep -q '^vm.swappiness' /etc/sysctl.conf || echo 'vm.swappiness=10' >>/etc/sysctl.conf

log "packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates curl gnupg git ufw >/dev/null

log "docker"
if command -v docker >/dev/null 2>&1; then
    echo "  already installed: $(docker --version)"
else
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg |
        gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        >/etc/apt/sources.list.d/docker.list
    apt-get update -qq
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io \
        docker-buildx-plugin docker-compose-plugin >/dev/null
    echo "  installed: $(docker --version)"
fi
systemctl enable --now docker >/dev/null 2>&1 || true

log "service user (${SERVICE_USER})"
if id "$SERVICE_USER" >/dev/null 2>&1; then
    echo "  exists"
else
    adduser --disabled-password --gecos "" "$SERVICE_USER"
    echo "  created"
fi
usermod -aG docker "$SERVICE_USER"
# Carry root's authorized_keys over so the same key reaches the service user.
if [[ -f /root/.ssh/authorized_keys ]]; then
    install -d -m 700 -o "$SERVICE_USER" -g "$SERVICE_USER" "/home/$SERVICE_USER/.ssh"
    install -m 600 -o "$SERVICE_USER" -g "$SERVICE_USER" \
        /root/.ssh/authorized_keys "/home/$SERVICE_USER/.ssh/authorized_keys"
fi

log "firewall"
# Only SSH and the two web ports. The dashboard's own 8847 is never published —
# Caddy reaches it over the compose network, so it must not be open to the world.
ufw allow OpenSSH >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null
ufw status | sed 's/^/  /'

log "ssh hardening"
# Key auth only. This box holds broker credentials in .env and its IP is public;
# a password-guessable root login is the whole attack surface in one line.
SSHD_DROPIN=/etc/ssh/sshd_config.d/99-glassbox.conf
cat >"$SSHD_DROPIN" <<'EOF'
PasswordAuthentication no
PermitRootLogin prohibit-password
EOF
if [[ ! -s /root/.ssh/authorized_keys ]]; then
    echo "  REFUSING to disable passwords: /root/.ssh/authorized_keys is empty."
    echo "  Run ssh-copy-id first, or you will lock yourself out."
    rm -f "$SSHD_DROPIN"
else
    systemctl reload ssh 2>/dev/null || systemctl reload sshd
    echo "  password auth disabled, key auth only"
fi

log "done"
echo "  next: clone the repo, fill .env, docker compose up -d --build"
