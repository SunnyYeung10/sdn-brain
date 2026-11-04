#!/usr/bin/env bash
# One-shot, bounded deployment. Run from the read-only project share using sudo.
set -Eeuo pipefail
export DEBIAN_FRONTEND=noninteractive
SOURCE=/mnt/codex-sdn
STATUS=/mnt/codex-sdn-status
mkdir -p "$STATUS"
if ! mountpoint -q "$STATUS"; then
    mount -t vboxsf codex_sdn_status_20261102 "$STATUS"
fi
exec > >(tee -a "$STATUS/deployment.log") 2>&1
trap 'rc=$?; printf "failed (line %s, exit %s)\n" "$LINENO" "$rc" > "$STATUS/status.txt"; exit "$rc"' ERR
test "$(id -u)" = 0
printf 'preflight\n' > "$STATUS/status.txt"
date -Is
cat /etc/os-release
free -h
getent passwd sdn >/dev/null
for unit in codex-sdn-brain-20261102 codex-sdn-lab-20261102; do
    if systemctl is-active --quiet "$unit"; then
        printf 'Existing project unit %s is active; no automatic replacement.\n' "$unit"
        exit 1
    fi
done
if ss -ltn | awk '{print $4}' | grep -q ':8000$'; then
    printf 'Guest port 8000 is already occupied. No service was stopped.\n'
    exit 1
fi
printf 'installing dependencies\n' > "$STATUS/status.txt"
apt-get update
apt-get install -y python3 python3-venv python3-pip mininet openvswitch-switch iputils-arping iperf3 curl
if ! command -v docker >/dev/null; then
    apt-get install -y docker.io
fi
systemctl start docker openvswitch-switch
ovs-vsctl show > "$STATUS/ovs-before.txt"
docker ps --format '{{.Names}} {{.Image}} {{.Ports}}' > "$STATUS/docker-before.txt"
if ovs-vsctl list-br | grep -Eq '^csdn_s[1-6]$'; then
    printf 'Project bridges csdn_s1..csdn_s6 already exist; no network was removed.\n'
    exit 1
fi
project=$(mktemp -d /home/sdn/sdn-brain-20261102-XXXXXX)
cp -a "$SOURCE"/. "$project"/
printf '%s\n' "$project" > "$STATUS/project-path.txt"
cd "$project"
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m unittest discover -s tests -v
printf 'starting ONOS\n' > "$STATUS/status.txt"
if ! curl -fsS --max-time 5 -u onos:rocks http://127.0.0.1:8181/onos/v1/devices >/dev/null; then
    if ss -ltn | awk '{print $4}' | grep -Eq ':(8181|6653)$'; then
        printf 'ONOS/OpenFlow ports are occupied but default credentials do not work. Existing service preserved.\n'
        exit 1
    fi
    docker run -d --name codex-onos-20261102 --restart unless-stopped \
        -e ONOS_APPS=drivers,openflow,lldpprovider,hostprovider \
        -p 127.0.0.1:8181:8181 -p 127.0.0.1:6653:6653 onosproject/onos:2.7.0
fi
ready=0
for _ in $(seq 1 90); do
    if curl -fsS --max-time 4 -u onos:rocks http://127.0.0.1:8181/onos/v1/devices > "$STATUS/onos-devices.json"; then
        ready=1
        break
    fi
    sleep 2
done
test "$ready" = 1
curl -fsS --max-time 10 -u onos:rocks http://127.0.0.1:8181/onos/v1/applications > "$project/onos-apps-before.json"
.venv/bin/python lab/activate.py
printf 'starting six-switch topology\n' > "$STATUS/status.txt"
systemd-run --unit=codex-sdn-lab-20261102 --collect --working-directory="$project" \
    /usr/bin/python3 "$project/lab/topology.py" --headless --prefix csdn_ \
    --dpid-base 0x53444e0000000000 --state /run/codex-sdn-lab-20261102.json
ready=0
for _ in $(seq 1 60); do
    if test -f /run/codex-sdn-lab-20261102.json; then
        ready=1
        break
    fi
    sleep 2
done
if test "$ready" != 1; then
    journalctl -u codex-sdn-lab-20261102 --no-pager -n 70
    exit 1
fi
printf 'starting Sanic load-aware brain\n' > "$STATUS/status.txt"
.venv/bin/python - <<'PY'
import secrets
from pathlib import Path
token=secrets.token_urlsafe(32)
Path('.vm.env').write_text('SDN_MODE=onos\nSDN_HOST=0.0.0.0\nSDN_PORT=8000\nSDN_ALLOWED_HOSTS=127.0.0.1:18000,localhost:18000,127.0.0.1:8000\nSDN_API_TOKEN='+token+'\n')
Path('.vm.env').chmod(0o600)
Path('/mnt/codex-sdn-status/api-token').write_text(token)
PY
chown -R sdn:sdn "$project"
systemd-run --unit=codex-sdn-brain-20261102 --collect --uid=sdn \
    --working-directory="$project" --property="EnvironmentFile=$project/.vm.env" \
    "$project/.venv/bin/python" -m sdn.server
printf 'checking actual traffic\n' > "$STATUS/status.txt"
.venv/bin/python lab/validate_virtualbox.py --status "$STATUS"
printf 'ready\n' > "$STATUS/status.txt"
printf '\nSDN services are running. Windows URL: http://127.0.0.1:18000\n'
printf 'Project directory: %s\n' "$project"
printf 'API token is stored in %s/api-token (not printed in deployment logs).\n' "$STATUS"
printf 'Stop this lab: sudo systemctl stop codex-sdn-brain-20261102 codex-sdn-lab-20261102\n'
