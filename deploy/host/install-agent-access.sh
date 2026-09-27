#!/usr/bin/env bash
# Give the coding agent its restricted way onto this host. Run as root, by the
# owner, from an up-to-date checkout of main:
#
#   bash /home/glassbox/glassbox-ai-quant/deploy/host/install-agent-access.sh "ssh-ed25519 AAAA... gbx-agent"
#
# Creates the `gbxagent` user, whose single key can run only gbx-dispatch
# (a forced command with `restrict`: no shell, no pty, no forwarding), and a
# sudoers rule for that one program. The scripts are COPIED to /usr/local/sbin,
# so a later `git pull` never changes what the agent can run — re-running this
# script is the owner's act. Safe to re-run; it rewrites the same files.
set -euo pipefail

PUBKEY="${1:-}"
REPO=/home/glassbox/glassbox-ai-quant
USER_NAME=gbxagent

[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }
[[ $PUBKEY == ssh-ed25519\ * ]] || { echo "usage: $0 \"ssh-ed25519 AAAA... comment\"" >&2; exit 1; }
[[ $PUBKEY != *$'\n'* && $PUBKEY != *\"* ]] || { echo "one key, no quotes" >&2; exit 1; }

id "$USER_NAME" >/dev/null 2>&1 || useradd --system --create-home --shell /bin/sh "$USER_NAME"
passwd -l "$USER_NAME" >/dev/null

install -m 0755 -o root -g root "$REPO/deploy/host/gbx_dispatch.py" /usr/local/sbin/gbx-dispatch
install -m 0755 -o root -g root "$REPO/deploy/host/gbx-dispatch-shim" /usr/local/sbin/gbx-dispatch-shim

HOME_DIR=$(getent passwd "$USER_NAME" | cut -d: -f6)
install -d -m 0755 -o root -g root "$HOME_DIR/.ssh"
printf 'command="/usr/local/sbin/gbx-dispatch-shim",restrict %s\n' "$PUBKEY" > "$HOME_DIR/.ssh/authorized_keys"
chown root:root "$HOME_DIR/.ssh/authorized_keys"
chmod 0644 "$HOME_DIR/.ssh/authorized_keys"

RULE="$USER_NAME ALL=(root) NOPASSWD: /usr/local/sbin/gbx-dispatch"
TMP=$(mktemp)
echo "$RULE" > "$TMP"
visudo -cf "$TMP" >/dev/null
install -m 0440 -o root -g root "$TMP" /etc/sudoers.d/gbxagent
rm -f "$TMP"

touch /var/log/gbx-agent.log
chmod 0640 /var/log/gbx-agent.log

echo "installed: $USER_NAME -> gbx-dispatch ($(sha256sum /usr/local/sbin/gbx-dispatch | cut -c1-12))"
