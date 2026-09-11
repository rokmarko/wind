#!/usr/bin/env bash
#
# update_nesis_radar.sh
# ---------------------
# Cron entry point: fetch the newest OPERA radar composite and publish it as the
# Nesis EU weather texture.
#
#   */5 * * * * /home/rok/src/wind/update_nesis_radar.sh >> /var/log/nesis-radar.log 2>&1
#
# DBZH composites appear every 5 minutes, about 4 minutes after nominal time.
#
# The output is replaced atomically, which matters more than it looks: Nesis
# only refetches when Last-Modified advances, and WeatherImageRenderer::Update()
# silently drops an image that fails to decode.  Serving a half-written PNG would
# therefore cost a whole update cycle of stale radar, not just one bad frame.
# Any failure leaves the previous good image in place.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUTPUT="${NESIS_RADAR_OUTPUT:-$REPO/data/radar/eu/radar-1.png}"
PYTHON="$REPO/venv/bin/python"

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

# Read one PNG text chunk; empty if the file is missing or unreadable.
png_text() {
	"$PYTHON" -c 'import sys
from PIL import Image
print(Image.open(sys.argv[1]).text.get(sys.argv[2], ""))' "$1" "$2" 2>/dev/null || true
}

# One run at a time — a slow S3 fetch must not overlap the next cron tick.
exec 9>"${TMPDIR:-/tmp}/nesis-radar.lock"
flock -n 9 || { log "previous run still going, skipping"; exit 0; }

OUT_DIR="$(dirname "$OUTPUT")"
mkdir -p "$OUT_DIR"

# A SIGKILL (OOM, power loss) skips the EXIT trap and strands a staging file.
# A real run takes seconds, so anything older than an hour is debris.
find "$OUT_DIR" -maxdepth 1 -name '.radar-*.png' -mmin +60 -delete 2>/dev/null || true

WORK="$(mktemp -d)"
# Stage in the destination directory so the final mv is a same-filesystem
# rename, and therefore atomic.
STAGE="$(mktemp "$OUT_DIR/.radar-XXXXXX.png")"
trap 'rm -rf "$WORK"; rm -f "$STAGE"' EXIT

cd "$REPO"

log "fetching OPERA composite"
"$PYTHON" opera_radar_map.py --output "$WORK/opera_dbzh.png" --show-coverage uncovered

# Republishing an unchanged frame bumps Last-Modified, and every Nesis in the
# field would then re-download the same image. Skip instead, so this stays safe
# to run on a tighter schedule than the 5-minute composite cadence.
NEW_TS="$(png_text "$WORK/opera_dbzh.png" NominalTime)"
CUR_TS=""
[ -f "$OUTPUT" ] && CUR_TS="$(png_text "$OUTPUT" NominalTime)"
if [ -n "$NEW_TS" ] && [ "$NEW_TS" = "$CUR_TS" ]; then
	log "frame $NEW_TS already published, nothing to do"
	exit 0
fi

log "converting to Nesis texture"
"$PYTHON" nesis_radar_png.py "$WORK/opera_dbzh.png" --output "$STAGE" --supersample 1

chmod 644 "$STAGE"          # mktemp makes it 0600; the web server must read it
mv -f "$STAGE" "$OUTPUT"

log "published $OUTPUT ($(stat -c%s "$OUTPUT") bytes)"
