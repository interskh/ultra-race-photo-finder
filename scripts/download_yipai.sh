#!/usr/bin/env bash
# Usage: scripts/download_yipai.sh <orderId> [--concurrency N] [--page-delay S]
# Runs detached, keeps the Mac awake, resumable (rerun the same command to top up).
# Refuses order ids registered in a race: use scripts/download.sh <race> for those.
set -euo pipefail
cd "$(dirname "$0")/.."
order="$1"; shift
[ -d /Volumes/Ext1TB ] || { echo "external disk not mounted" >&2; exit 1; }
uv run --frozen python -c 'import sys; from photofinder.sources import yipai; sys.exit(yipai.refusal(sys.argv[1]))' "$order"
root="${PHOTOFINDER_DATA_ROOT:-data}"
out="$root/yipai/$order"
mkdir -p "$out"
nohup caffeinate -ims uv run --frozen python -m photofinder.sources.yipai "$order" --data-root "$root" "$@" >> "$out/console.log" 2>&1 &
echo "pid $! — tail -f $out/download.log"
