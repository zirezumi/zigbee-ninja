#!/bin/bash
# Install ninja-tap as a systemd service on a capture host. Idempotent.
# The collector GUI renders a ready-filled invocation; the raw form is:
#   install.sh <collector-ws-url> <token> [bpf-filter] [iface]
# e.g. install.sh ws://10.0.0.5:8686/api/ws/tap TOKEN "tcp port 6638" vmbr0
set -euo pipefail

COLLECTOR="${1:?collector ws:// URL required}"
TOKEN="${2:?agent token required}"
FILTER="${3:-tcp port 6638}"
IFACE="${4:-any}"

SRC="$(cd "$(dirname "$0")" && pwd)"
install -d /opt/ninja-tap /etc/ninja-tap
install -m 0755 "$SRC/ninja-tap.py" /opt/ninja-tap/ninja-tap.py
umask 077
printf '%s' "$TOKEN" > /etc/ninja-tap/token
cat > /etc/ninja-tap/agent.env <<EOF
ZN_COLLECTOR=$COLLECTOR
ZN_FILTER=$FILTER
ZN_IFACE=$IFACE
EOF
install -m 0644 "$SRC/ninja-tap.service" /etc/systemd/system/ninja-tap.service

command -v tcpdump >/dev/null || { echo "installing tcpdump"; apt-get -qq update && apt-get -qq install -y tcpdump; }
systemctl daemon-reload
systemctl enable --now ninja-tap.service
sleep 2
systemctl --no-pager --lines=5 status ninja-tap.service || true
echo "Uninstall: systemctl disable --now ninja-tap && rm -rf /opt/ninja-tap /etc/ninja-tap /etc/systemd/system/ninja-tap.service"
