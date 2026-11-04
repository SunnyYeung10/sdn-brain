#!/usr/bin/env bash
set -Eeuo pipefail
SOURCE=/mnt/codex-sdn
STATUS=/mnt/codex-sdn-status
test "$(id -u)" = 0
mountpoint -q "$SOURCE"
mountpoint -q "$STATUS"
exec > >(tee -a "$STATUS/repair.log") 2>&1
trap 'rc=$?; printf "repair failed (line %s, exit %s)\n" "$LINENO" "$rc" > "$STATUS/status.txt"; exit "$rc"' ERR
project=$(cat "$STATUS/project-path.txt")
case "$project" in /home/sdn/sdn-brain-20261102-*) ;; *) exit 1 ;; esac
test -d "$project/.venv"
printf 'updating permanent-flow and batch-install fixes\n' > "$STATUS/status.txt"
systemctl stop codex-sdn-brain-20261102
for area in sdn tests web; do
    cp -a "$SOURCE/$area"/. "$project/$area/"
done
cp "$SOURCE/lab/validate_virtualbox.py" "$project/lab/validate_virtualbox.py"
cp "$SOURCE/lab/load_validate_virtualbox.py" "$project/lab/load_validate_virtualbox.py"
chown -R sdn:sdn "$project"
cd "$project"
.venv/bin/python -m unittest discover -s tests -v
systemd-run --unit=codex-sdn-brain-20261102 --collect --uid=sdn \
    --working-directory="$project" --property="EnvironmentFile=$project/.vm.env" \
    "$project/.venv/bin/python" -m sdn.server
printf 'verifying actual unicast and multicast\n' > "$STATUS/status.txt"
.venv/bin/python lab/validate_virtualbox.py --status "$STATUS"
printf 'verifying measured-load reroute\n' > "$STATUS/status.txt"
.venv/bin/python -m lab.load_validate_virtualbox --status "$STATUS"
printf 'ready\n' > "$STATUS/status.txt"

# Final browser compatibility sync; does not restart the running dataplane.
printf 'syncing browser token form\n' > "$STATUS/status.txt"
cp "$SOURCE/web/index.html" "$project/web/index.html"
cp "$SOURCE/web/app.js" "$project/web/app.js"
chown sdn:sdn "$project/web/index.html" "$project/web/app.js"
printf 'ready\n' > "$STATUS/status.txt"
printf 'Final browser token form synchronized.\n'
