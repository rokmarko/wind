#!/usr/bin/env python3
"""
opera_radar_map.py
------------------
Fetches the latest EUMETNET OPERA European weather-radar composite and writes a
colourised RGBA PNG of radar reflectivity — a rain-radar picture of Europe that
can be viewed directly or overlaid on a basemap.

Unlike the wind scripts in this repo, the channels are *not* an encoding: the
PNG carries a normal reflectivity colour scale, and is fully transparent
wherever it is not raining or the radar network has no coverage.

  RGB  = NWS reflectivity colour ramp applied to dBZ
  A    = 0 outside radar coverage, 0 below --min-dbz, 255 for real echo

The image is written on the composite's **native grid**, 1:1, with no
resampling.  For the DBZH product that is 3800 × 4400 pixels at 1 km, in a
Lambert Azimuthal Equal Area projection:

  +proj=laea +lat_0=55.0 +lon_0=10.0 +x_0=1950000.0 +y_0=-2100000.0
  +units=m +ellps=WGS84

Row 0 is the northern edge.  The projection definition and the four corner
lat/lons are copied into the PNG text metadata so a consumer can georeference
the image without re-reading the source file.

Data source
-----------
  EUMETNET OPERA / RODEO "Open Radar Data" 24-hour rolling cache, served
  anonymously from CloudFerro S3.  No API key and no rate limit — the
  MeteoGate REST gateway is not used.

    https://s3.waw3-1.cloudferro.com/openradar-24h/
        {YYYY}/{MM}/{DD}/OPERA/COMP/OPERA@{YYYYMMDD}T{HHMM}@0@DBZH.h5

  Licence: CC BY 4.0 — attribution to EUMETNET OPERA is required when the
  output image is published.

Usage
-----
  pip install h5py pillow numpy requests
  python opera_radar_map.py [--output opera_dbzh.png] [--scale 2]
                            [--min-dbz 5] [--show-coverage [covered|uncovered]]
                            [--time YYYYMMDDTHHMM] [--list]

Dependencies
------------
  h5py     – ODIM_H5 (HDF5) reader
  Pillow   – PNG output
  numpy    – everything numeric
  requests – anonymous S3 listing and download
"""

import argparse
import datetime
import re
import sys
import tempfile
from pathlib import Path

import numpy as np

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BUCKET_URL = "https://s3.waw3-1.cloudferro.com/openradar-24h"
PRODUCT = "DBZH"
COMPOSITE_PREFIX = "OPERA/COMP"

# How many hourly listing prefixes to scan backwards when looking for the
# newest frame.  Composites appear ~4 minutes after nominal time; a handful of
# hours is plenty and keeps each listing well under the 1000-key page limit.
LOOKBACK_HOURS = 6

# Classic NWS reflectivity colour scale, linearly interpolated between stops.
DBZ_COLOUR_STOPS: tuple[tuple[float, tuple[int, int, int]], ...] = (
    ( 5.0, (0x04, 0xE9, 0xE7)),   # light cyan
    (10.0, (0x01, 0x9F, 0xF4)),
    (15.0, (0x03, 0x00, 0xF4)),   # blue
    (20.0, (0x02, 0xFD, 0x02)),   # green
    (25.0, (0x01, 0xC5, 0x01)),
    (30.0, (0x00, 0x8E, 0x00)),
    (35.0, (0xFD, 0xF8, 0x02)),   # yellow
    (40.0, (0xE5, 0xBC, 0x00)),
    (45.0, (0xFD, 0x95, 0x00)),   # orange
    (50.0, (0xFD, 0x00, 0x00)),   # red
    (55.0, (0xD4, 0x00, 0x00)),
    (60.0, (0xBC, 0x00, 0x00)),
    (65.0, (0xF8, 0x00, 0xFD)),   # magenta
    (70.0, (0x98, 0x54, 0xC6)),   # violet
    (75.0, (0xFD, 0xFD, 0xFD)),   # white
)

# dBZ span over which alpha ramps 0 → 255 just above the display threshold,
# so faint echo fades in instead of appearing with a hard edge.
ALPHA_RAMP_DBZ = 5.0

# Tints used by --show-coverage.  The two variants mark opposite things, so they
# get different hues: somebody holding both images should not have to remember
# which grey meant what.
COVERED_RGB = (60, 60, 70)          # radar sees here, it is simply not raining
COVERED_ALPHA = 20
UNCOVERED_RGB = (74, 56, 56)        # no radar here — absence of echo means nothing
UNCOVERED_ALPHA = 20


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="EUMETNET OPERA radar composite → colourised RGBA PNG over Europe"
    )
    p.add_argument(
        "--output", type=str, default="opera_dbzh.png",
        help="Output PNG file path (default: opera_dbzh.png)"
    )
    p.add_argument(
        "--time", type=str, default=None,
        help=(
            "Fetch one specific frame instead of the newest, as YYYYMMDDTHHMM "
            "on the 5-minute grid, e.g. 20260910T0900. Only the last 24 hours "
            "are kept in the cache."
        ),
    )
    p.add_argument(
        "--scale", type=int, default=1,
        help=(
            "Integer block reduction factor (default: 1 = native 1 km grid). "
            "Blocks are combined with a maximum, not a mean, so storm cores "
            "survive. '--scale 2' gives 1900×2200 at 2 km."
        ),
    )
    p.add_argument(
        "--min-dbz", type=float, default=5.0,
        help=(
            "Reflectivity below this is drawn fully transparent (default: 5.0). "
            "Suppresses clutter and clear-air noise."
        ),
    )
    p.add_argument(
        "--show-coverage", nargs="?", const="covered", default=None,
        choices=["covered", "uncovered"],
        help=(
            "Faintly tint one side of the radar footprint, so that 'no rain' "
            "can be told apart from 'no radar'. 'covered' (the default when the "
            "flag is given bare) tints in-coverage-but-dry pixels, showing where "
            "the network reaches; 'uncovered' inverts that and tints the gaps "
            "instead, showing where an empty map means nothing was looked at."
        ),
    )
    p.add_argument(
        "--list", action="store_true",
        help="List the composite timestamps available in the cache and exit"
    )
    return p.parse_args()


# ---------------------------------------------------------------------------
# S3 discovery (anonymous, no API key)
# ---------------------------------------------------------------------------

def _hour_prefixes(hours: int) -> list[str]:
    """
    Return listing prefixes for the last *hours* whole UTC hours, newest first.
    Each prefix is scoped to a single hour so the listing stays small and the
    day rollover at midnight is handled for free.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    prefixes = []
    for back in range(hours):
        t = now - datetime.timedelta(hours=back)
        prefixes.append(
            f"{t:%Y/%m/%d}/{COMPOSITE_PREFIX}/OPERA@{t:%Y%m%d}T{t:%H}"
        )
    return prefixes


def _list_keys(prefix: str, product: str) -> list[str]:
    """
    List the ODIM_H5 object keys under *prefix* belonging to *product*.
    Returns them sorted ascending; the key timestamp sorts lexicographically.
    """
    try:
        import requests
    except ImportError:
        sys.exit("requests is not installed.\nRun:  pip install requests")

    resp = requests.get(
        BUCKET_URL,
        params={"list-type": "2", "prefix": prefix, "max-keys": "1000"},
        timeout=30,
    )
    resp.raise_for_status()
    keys = re.findall(r"<Key>([^<]+)</Key>", resp.text)
    suffix = f"@0@{product}.h5"
    return sorted(k for k in keys if k.endswith(suffix))


def list_recent_keys(product: str = PRODUCT, hours: int = LOOKBACK_HOURS) -> list[str]:
    """Every available key for *product* over the last *hours*, oldest first."""
    keys: list[str] = []
    for prefix in _hour_prefixes(hours):
        keys.extend(_list_keys(prefix, product))
    return sorted(set(keys))


def latest_composite_key(product: str = PRODUCT) -> str:
    """
    Find the newest available composite, walking hourly listing prefixes from
    most-recent backwards and stopping at the first one that has any data.
    """
    for prefix in _hour_prefixes(LOOKBACK_HOURS):
        print(f"[s3]    Listing {prefix}* …")
        keys = _list_keys(prefix, product)
        if keys:
            return keys[-1]
        print("[s3]    Empty, trying the previous hour …")
    else:
        sys.exit(
            f"[s3]    No {product} composite found in the last {LOOKBACK_HOURS} "
            "hours. The Open Radar Data cache may be temporarily unavailable."
        )


def key_for_time(time_str: str, product: str = PRODUCT) -> str:
    """Build the object key for an explicit YYYYMMDDTHHMM timestamp."""
    try:
        t = datetime.datetime.strptime(time_str, "%Y%m%dT%H%M")
    except ValueError:
        sys.exit(
            f"Invalid --time value '{time_str}'. "
            "Use YYYYMMDDTHHMM, e.g. 20260910T0900."
        )
    return (
        f"{t:%Y/%m/%d}/{COMPOSITE_PREFIX}/"
        f"OPERA@{t:%Y%m%d}T{t:%H%M}@0@{product}.h5"
    )


def fetch_composite(key: str, out_dir: Path) -> Path:
    """Download one composite object into *out_dir* and return its path."""
    try:
        import requests
    except ImportError:
        sys.exit("requests is not installed.\nRun:  pip install requests")

    url = f"{BUCKET_URL}/{key}"
    print(f"[opera] Downloading {url} …")
    resp = requests.get(url, timeout=120)
    if resp.status_code == 404:
        sys.exit(
            f"[opera] Not found: {url}\n"
            "The 24-hour cache may have expired that frame, or the timestamp "
            "is not on the product's 5-minute grid."
        )
    resp.raise_for_status()

    path = out_dir / Path(key).name
    path.write_bytes(resp.content)
    print(f"[opera] Got {len(resp.content) / 1e6:.1f} MB")
    return path


# ---------------------------------------------------------------------------
# ODIM_H5 → numpy array
# ---------------------------------------------------------------------------

def _attr(group, name: str, default=None):
    """Read one HDF5 attribute, decoding ODIM's byte strings to str."""
    if name not in group.attrs:
        return default
    value = group.attrs[name]
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, np.generic):
        value = value.item()
        if isinstance(value, bytes):
            return value.decode("utf-8", "replace")
    return value


def odim_to_array(path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray, dict]:
    """
    Read an ODIM_H5 composite and return
    (values, nodata_mask, undetect_mask, meta).

    values         – 2-D float32, physical units (dBZ for DBZH), rows north→south
    nodata_mask    – True outside the radar network's coverage
    undetect_mask  – True inside coverage but nothing detected (dry)
    meta           – dict of the ODIM /what, /where, /how and /dataset1/what
                     attributes worth carrying into the PNG
    """
    try:
        import h5py
    except ImportError:
        sys.exit(
            "h5py is not installed.\n"
            "Run:  pip install h5py"
        )

    with h5py.File(path, "r") as f:
        data_group = f["/dataset1/data1"]
        raw = data_group["data"][:]

        what = data_group["what"]
        gain = float(_attr(what, "gain", 1.0))
        offset = float(_attr(what, "offset", 0.0))
        nodata = _attr(what, "nodata")
        undetect = _attr(what, "undetect")

        # Compare against the *raw* stored values, before gain/offset scaling.
        nodata_mask = (raw == nodata) if nodata is not None else np.zeros(raw.shape, bool)
        undetect_mask = (raw == undetect) if undetect is not None else np.zeros(raw.shape, bool)

        values = (raw * gain + offset).astype(np.float32)

        root_what, where, how = f["/what"], f["/where"], f["/how"]
        ds_what = f["/dataset1/what"]
        nodes = _attr(how, "nodes", "")

        meta = {
            "quantity":   _attr(what, "quantity", PRODUCT),
            "conventions": _attr(f, "Conventions", ""),
            "prodname":   _attr(ds_what, "prodname", ""),
            "product":    _attr(ds_what, "product", ""),
            "date":       _attr(root_what, "date", ""),
            "time":       _attr(root_what, "time", ""),
            "startdate":  _attr(ds_what, "startdate", ""),
            "starttime":  _attr(ds_what, "starttime", ""),
            "enddate":    _attr(ds_what, "enddate", ""),
            "endtime":    _attr(ds_what, "endtime", ""),
            "projdef":    _attr(where, "projdef", ""),
            "xsize":      int(_attr(where, "xsize", raw.shape[1])),
            "ysize":      int(_attr(where, "ysize", raw.shape[0])),
            "xscale":     float(_attr(where, "xscale", 0.0)),
            "yscale":     float(_attr(where, "yscale", 0.0)),
            "creator":    _attr(how, "creator_name", ""),
            "publisher":  _attr(how, "publisher_name", ""),
            "license":    _attr(how, "license", ""),
            "node_count": len([n for n in nodes.split(",") if n.strip()]),
        }
        for corner in ("UL", "UR", "LL", "LR"):
            meta[f"{corner}_lat"] = float(_attr(where, f"{corner}_lat", 0.0))
            meta[f"{corner}_lon"] = float(_attr(where, f"{corner}_lon", 0.0))

    return values, nodata_mask, undetect_mask, meta


def odim_epoch(date: str, time: str) -> str:
    """Seconds since the Unix epoch for an ODIM YYYYMMDD / HHMMSS pair ("" if unparsable)."""
    try:
        t = datetime.datetime.strptime(f"{date}{time:0<6}", "%Y%m%d%H%M%S")
    except ValueError:
        return ""
    return str(int(t.replace(tzinfo=datetime.timezone.utc).timestamp()))


# ---------------------------------------------------------------------------
# Optional block reduction
# ---------------------------------------------------------------------------

def _blocks(a: np.ndarray, scale: int) -> np.ndarray:
    """Reshape *a* into (H//scale, scale, W//scale, scale), trimming remainders."""
    h = a.shape[0] // scale * scale
    w = a.shape[1] // scale * scale
    return a[:h, :w].reshape(h // scale, scale, w // scale, scale)


def block_max(
    values: np.ndarray,
    nodata_mask: np.ndarray,
    undetect_mask: np.ndarray,
    scale: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Reduce the grid by an integer factor, combining each block with a *maximum*
    over its valid cells so storm cores are not averaged away.

    A block is valid if any cell is valid, dry if none are valid but some are
    undetect, and out-of-coverage only if every cell is out of coverage.
    """
    if scale <= 1:
        return values, nodata_mask, undetect_mask

    valid = ~(nodata_mask | undetect_mask)
    masked = np.where(valid, values, -np.inf)

    reduced = _blocks(masked, scale).max(axis=(1, 3))
    any_valid = _blocks(valid, scale).any(axis=(1, 3))
    any_undetect = _blocks(undetect_mask, scale).any(axis=(1, 3))

    out_values = np.where(any_valid, reduced, 0.0).astype(np.float32)
    out_undetect = ~any_valid & any_undetect
    out_nodata = ~any_valid & ~any_undetect
    return out_values, out_nodata, out_undetect


# ---------------------------------------------------------------------------
# Colourise dBZ → RGBA
# ---------------------------------------------------------------------------

def colorize_dbzh(
    values: np.ndarray,
    nodata_mask: np.ndarray,
    undetect_mask: np.ndarray,
    min_dbz: float,
    show_coverage: str | None = None,
) -> np.ndarray:
    """
    Map reflectivity to the NWS colour scale and build the alpha channel.

    Returns uint8 array of shape (H, W, 4).  Alpha is 0 wherever there is no
    coverage, nothing was detected, or the echo is below *min_dbz*; it ramps in
    over the next ALPHA_RAMP_DBZ dB so faint echo has no hard edge.

    *show_coverage* optionally tints one of the two transparent states:
    'covered' marks in-coverage-but-dry pixels, 'uncovered' marks the gaps in
    the network.  Either way the echo itself is untouched.
    """
    stops = np.array([s for s, _ in DBZ_COLOUR_STOPS], dtype=np.float32)
    colours = np.array([c for _, c in DBZ_COLOUR_STOPS], dtype=np.float32)

    # Masked cells hold fill values like -9999000; clamp them to the bottom of
    # the ramp so the interpolation never sees them.  Their alpha is 0 anyway.
    blank = nodata_mask | undetect_mask
    dbz = np.where(blank, min_dbz, values)

    rgba = np.empty(dbz.shape + (4,), dtype=np.uint8)
    for channel in range(3):
        # np.interp clamps outside the stop range, which is exactly what we
        # want: below 5 dBZ takes the first colour, above 75 dBZ stays white.
        rgba[..., channel] = np.interp(dbz, stops, colours[:, channel]).astype(np.uint8)

    alpha = np.clip((dbz - min_dbz) / ALPHA_RAMP_DBZ, 0.0, 1.0) * 255.0
    alpha[blank] = 0.0
    rgba[..., 3] = alpha.astype(np.uint8)

    if show_coverage == "covered":
        rgba[undetect_mask, 0:3] = COVERED_RGB
        rgba[undetect_mask, 3] = COVERED_ALPHA
    elif show_coverage == "uncovered":
        rgba[nodata_mask, 0:3] = UNCOVERED_RGB
        rgba[nodata_mask, 3] = UNCOVERED_ALPHA

    return rgba


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()

    try:
        from ecmwf_wind_map import save_png
    except ImportError as exc:
        sys.exit(
            f"Cannot import from ecmwf_wind_map.py: {exc}\n"
            "Run this script from the repository directory."
        )

    if args.list:
        keys = list_recent_keys()
        if not keys:
            sys.exit(f"[s3]    No {PRODUCT} composite found in the last {LOOKBACK_HOURS} hours.")
        print(f"[s3]    {len(keys)} {PRODUCT} frames in the last {LOOKBACK_HOURS} hours:")
        for key in keys:
            print(f"          {Path(key).name.split('@')[1]}")
        return

    if args.scale < 1:
        sys.exit(f"--scale must be a positive integer, got {args.scale}.")

    # 1. Locate and download
    key = key_for_time(args.time) if args.time else latest_composite_key()
    with tempfile.TemporaryDirectory(prefix="opera_") as tmp:
        h5_path = fetch_composite(key, Path(tmp))

        # 2. Decode
        print("[decode] Reading ODIM_H5 composite …")
        values, nodata_mask, undetect_mask, meta = odim_to_array(h5_path)

    valid = ~(nodata_mask | undetect_mask)
    print(f"[decode] {meta['prodname']}")
    print(f"         nominal time : {meta['date']} {meta['time']} UTC")
    print(f"         grid         : {meta['xsize']} × {meta['ysize']} @ {meta['xscale']:.0f} m")
    print(f"         radar nodes  : {meta['node_count']}")
    print(f"         coverage     : {(~nodata_mask).mean() * 100:.1f}% of the grid")
    print(f"         echo         : {valid.mean() * 100:.1f}% of the grid")
    if valid.any():
        print(f"         dBZ min/max  : {values[valid].min():.1f} / {values[valid].max():.1f}")

    # 3. Optional reduction
    if args.scale > 1:
        print(f"[decode] Reducing by {args.scale}× (block maximum) …")
        values, nodata_mask, undetect_mask = block_max(
            values, nodata_mask, undetect_mask, args.scale
        )

    # 4. Colourise
    print(f"[color]  Applying reflectivity colour scale (≥ {args.min_dbz:g} dBZ) …")
    rgba = colorize_dbzh(
        values, nodata_mask, undetect_mask, args.min_dbz, args.show_coverage
    )
    print(f"[color]  {(rgba[..., 3] == 0).mean() * 100:.1f}% of pixels fully transparent")

    # 5. Save
    ramp = " ".join(f"{s:g}dBZ=#{r:02x}{g:02x}{b:02x}" for s, (r, g, b) in DBZ_COLOUR_STOPS)
    png_meta = {
        "Description":  f"EUMETNET OPERA {meta['quantity']} radar composite over Europe",
        "ProdName":      meta["prodname"],
        "Product":       meta["quantity"],
        "NominalTime":  f"{meta['date']}T{meta['time']}Z",
        "NominalTS":     odim_epoch(meta["date"], meta["time"]),
        "StartTime":    f"{meta['startdate']}T{meta['starttime']}Z",
        "EndTime":      f"{meta['enddate']}T{meta['endtime']}Z",
        "Projection":    meta["projdef"],
        "Corner_UL":    f"{meta['UL_lat']:.6f},{meta['UL_lon']:.6f}",
        "Corner_UR":    f"{meta['UR_lat']:.6f},{meta['UR_lon']:.6f}",
        "Corner_LL":    f"{meta['LL_lat']:.6f},{meta['LL_lon']:.6f}",
        "Corner_LR":    f"{meta['LR_lat']:.6f},{meta['LR_lon']:.6f}",
        "CornerOrder":   "Row 0 is the northern edge; column 0 is the western edge",
        "GridSize":     f"{rgba.shape[1]}x{rgba.shape[0]}",
        "PixelSize_m":  f"{meta['xscale'] * args.scale:.0f}",
        "MinDBZ":        str(args.min_dbz),
        "CoverageTint":  args.show_coverage or "none",
        "Colormap":      ramp,
        "RadarNodes":    str(meta["node_count"]),
        "SourceURL":    f"{BUCKET_URL}/{key}",
        "Creator":       meta["creator"],
        "License":      f"CC BY 4.0 — EUMETNET OPERA / RODEO ({meta['license']})",
    }
    save_png(rgba, args.output, png_meta)

    print()
    print("Attribution required by the CC BY 4.0 licence when publishing this image:")
    print("  © EUMETNET OPERA — Open Radar Data (RODEO), CC BY 4.0")


if __name__ == "__main__":
    main()
