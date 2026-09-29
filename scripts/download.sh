#!/usr/bin/env bash
# Usage: scripts/download.sh <race> [album-key]
# Downloads a registered race's albums one after another, detached, keeping the Mac awake.
# Resumable: rerun the same command to top up.
set -euo pipefail
cd "$(dirname "$0")/.."
[ $# -ge 1 ] || { echo "usage: scripts/download.sh <race> [album-key]" >&2; exit 1; }
[ -d /Volumes/Ext1TB ] || { echo "external disk not mounted" >&2; exit 1; }
uv run --frozen python -c 'import sys; from photofinder import cli; cli.select_albums(*sys.argv[1:])' "$@"
race="$1"
dir="${PHOTOFINDER_DATA_ROOT:-data}/races/$race"
mkdir -p "$dir"
nohup caffeinate -ims uv run --frozen photofinder download "$@" >> "$dir/download-console.log" 2>&1 &
echo "pid $! — tail -f $dir/download-console.log (per album: $dir/albums/<key>/download.log)"
