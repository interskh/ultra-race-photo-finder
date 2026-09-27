#!/usr/bin/env bash
# Usage: scripts/download_yipai.sh <orderId> [--concurrency N] [--page-delay S]
# Runs detached, keeps the Mac awake, resumable (rerun the same command to top up).
set -euo pipefail
cd "$(dirname "$0")/.."
order="$1"; shift
[ -d /Volumes/Ext1TB ] || { echo "external disk not mounted" >&2; exit 1; }
out="data/yipai/$order"
mkdir -p "$out"
nohup caffeinate -ims uv run --frozen python -m photofinder.sources.yipai "$order" "$@" >> "$out/console.log" 2>&1 &
echo "pid $! — tail -f $out/download.log"
