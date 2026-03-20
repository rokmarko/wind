#!/usr/bin/env python3
"""
ecmwf_wind_map.py
-----------------
Fetches the latest global wind forecast (U and V components) at a selectable
altitude/pressure level and writes a 2048×1024 PNG where:

  R channel  = U-component  (west→east),  normalised to [0, 255]  (128 = 0 m/s)
  G channel  = V-component  (south→north), normalised to [0, 255]  (128 = 0 m/s)
  B channel  = wind speed magnitude,       normalised to [0, 255]  (255 = MAX_SPEED)

The image covers the whole globe in equirectangular projection:
  x = 0..2047  →  lon  −180° .. +180°
  y = 0..1023  →  lat  +90°  .. −90°   (top = north)

Supported models
----------------
  ecmwf  – ECMWF Open Data HRES (default, ~9 km, 0.1°)
  gfs    – NOAA GFS 0.125° (downloaded from NOMADS via byte-range requests)

Usage
-----
  pip install ecmwf-opendata cfgrib xarray scipy pillow numpy requests
  python ecmwf_wind_map.py [--model ecmwf|gfs] [--step 0] [--output wind.png] \
                           [--max-speed 50] [--level 10m|<hPa>]

Dependencies
------------
  ecmwf-opendata   – zero-auth download of ECMWF open HRES forecasts
  cfgrib / eccodes – GRIB2 decoder (also needs the eccodes C library)
  xarray           – labelled arrays
  scipy            – map_coordinates for bilinear resampling
  Pillow           – PNG output
  numpy            – everything numeric
  requests         – HTTP byte-range downloads for GFS (pip install requests)
"""

import argparse
import datetime
import sys
import tempfile
from pathlib import Path

import numpy as np

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Global wind forecast → 2048×1024 UV-encoded PNG"
    )
    p.add_argument(
        "--model", type=str, default="ecmwf",
        choices=["ecmwf", "gfs"],
        help=(
            "Data source: 'ecmwf' = ECMWF Open Data HRES (default), "
            "'gfs' = NOAA GFS 0.125° (NOMADS)"
        ),
    )
    p.add_argument(
        "--step", type=int, default=0,
        help="Forecast step in hours (0 = analysis/T+0, default: 0)"
    )
    p.add_argument(
        "--level", type=str, default="10m",
        help=(
            "Wind level to fetch. Use '10m' for 10-metre surface wind (default), "
            "or an integer pressure level in hPa, e.g. 500, 850, 250. "
            "Common levels: 1000 925 850 700 500 300 250 200 100 50."
        ),
    )
    p.add_argument(
        "--output", type=str, default="wind_uv.png",
        help="Output PNG file path (default: wind_uv.png)"
    )
    p.add_argument(
        "--max-speed", type=float, default=50.0,
        help="Wind speed (m/s) that maps to B=255; clipped above (default: 50)"
    )
    p.add_argument(
        "--max-uv", type=float, default=50.0,
        help="±max m/s for U/V normalisation to [0,255] (default: 50)"
    )
    p.add_argument(
        "--width", type=int, default=2048,
        help="Output image width  in pixels (default: 2048)"
    )
    p.add_argument(
        "--height", type=int, default=1024,
        help="Output image height in pixels (default: 1024)"
    )
    return p.parse_args()


# ---------------------------------------------------------------------------
# ECMWF Open Data download
# ---------------------------------------------------------------------------


def _parse_level(level_str: str) -> tuple[str, str, int | None]:
    """
    Parse the --level argument.
    Returns (u_param, v_param, pressure_hpa_or_None).
    '10m'  → ('10u', '10v', None)   – surface level
    '500'  → ('u',   'v',  500)     – pressure level in hPa
    """
    if level_str.lower() == "10m":
        return "10u", "10v", None
    try:
        hpa = int(level_str)
    except ValueError:
        sys.exit(
            f"Invalid --level value '{level_str}'. "
            "Use '10m' or an integer pressure level in hPa (e.g. 500)."
        )
    if hpa <= 0:
        sys.exit(f"Pressure level must be a positive integer, got {hpa}.")
    return "u", "v", hpa


def fetch_wind_grib(step: int, level: str, out_dir: Path) -> tuple[Path, Path]:
    """
    Download the latest available HRES forecast for the U and V wind components
    at the requested level ('10m' or a pressure level in hPa).
    Returns paths to the two GRIB files (u, v).
    """
    try:
        from ecmwf.opendata import Client
    except ImportError:
        sys.exit(
            "ecmwf-opendata is not installed.\n"
            "Run:  pip install ecmwf-opendata"
        )

    u_param, v_param, pressure = _parse_level(level)
    client = Client(source="ecmwf")          # real-time HRES, no API key needed

    u_path = out_dir / "u.grib2"
    v_path = out_dir / "v.grib2"

    level_desc = f"{pressure} hPa" if pressure is not None else "10 m"
    base_kwargs: dict = {"type": "fc", "step": step}
    if pressure is not None:
        base_kwargs["levtype"] = "pl"
        base_kwargs["levelist"] = pressure

    print(f"[ecmwf] Fetching {u_param} at {level_desc}, step={step}h …")
    client.retrieve(**base_kwargs, param=u_param, target=str(u_path))

    print(f"[ecmwf] Fetching {v_param} at {level_desc}, step={step}h …")
    client.retrieve(**base_kwargs, param=v_param, target=str(v_path))

    return u_path, v_path


# ---------------------------------------------------------------------------
# GFS 0.125° download (NOAA NOMADS, byte-range via .idx)
# ---------------------------------------------------------------------------

def _gfs_cycle_candidates() -> list[tuple[str, str]]:
    """
    Return a list of (date_str 'YYYYMMDD', cycle_str '00'|'06'|'12'|'18')
    in order from most-recent to oldest, covering all GFS runs from the past
    3 days whose nominal start time is at least 5 hours in the past.
    The caller should try them in order and skip any that return HTTP 404.
    """
    now = datetime.datetime.utcnow()
    candidates: list[tuple[str, str]] = []
    for delta_days in range(3):
        day = now - datetime.timedelta(days=delta_days)
        for cycle in [18, 12, 6, 0]:
            run_time = day.replace(hour=cycle, minute=0, second=0, microsecond=0)
            age_hours = (now - run_time).total_seconds() / 3600
            if age_hours >= 3.5:
                candidates.append((run_time.strftime("%Y%m%d"), f"{cycle:02d}"))
    if not candidates:
        fallback = now - datetime.timedelta(days=2)
        candidates.append((fallback.strftime("%Y%m%d"), "18"))
    return candidates


def _gfs_idx_search_strings(level_str: str) -> tuple[str, str]:
    """
    Return the substring patterns used to locate U and V records inside a GFS
    GRIB2 index (.idx) file.
    '10m'  → ('UGRD:10 m above ground', 'VGRD:10 m above ground')
    '500'  → ('UGRD:500 mb',            'VGRD:500 mb')
    """
    if level_str.lower() == "10m":
        return "UGRD:10 m above ground", "VGRD:10 m above ground"
    try:
        hpa = int(level_str)
    except ValueError:
        sys.exit(f"Invalid --level value '{level_str}' for GFS. Use '10m' or an integer hPa level.")
    if hpa <= 0:
        sys.exit(f"Pressure level must be a positive integer, got {hpa}.")
    return f"UGRD:{hpa} mb", f"VGRD:{hpa} mb"


def fetch_wind_grib_gfs(step: int, level: str, out_dir: Path) -> tuple[Path, Path]:
    """
    Download U and V wind components from the NOAA NOMADS GFS 0.125° dataset.
    Uses the companion .idx index file for efficient byte-range downloads so
    only the two required records are transferred.
    Tries candidate cycles from most-recent to oldest and skips cycles whose
    index files return HTTP 404 (data not yet posted or already expired).
    """
    try:
        import requests
    except ImportError:
        sys.exit("requests is not installed.\nRun:  pip install requests")

    u_search, v_search = _gfs_idx_search_strings(level)
    level_desc = f"{level} hPa" if level.lower() != "10m" else "10 m"
    step_str = f"{step:03d}"

    idx_lines: list[str] = []
    base_url = ""
    for date_str, cycle in _gfs_cycle_candidates():
        base_url = (
            f"https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/prod/"
            f"gfs.{date_str}/{cycle}/atmos/"
            f"gfs.t{cycle}z.pgrb2.0p25.f{step_str}"
        )
        idx_url = base_url + ".idx"
        print(f"[gfs]  Trying run: {date_str} T{cycle}Z  step=+{step}h  level={level_desc}")
        print(f"[gfs]  Index: {idx_url}")
        try:
            r = requests.get(idx_url, timeout=30)
            r.raise_for_status()
        except requests.exceptions.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                print("[gfs]  Not found (404), trying previous cycle …")
                continue
            raise
        idx_lines = r.text.splitlines()
        print(f"[gfs]  Using run: {date_str} T{cycle}Z")
        break
    else:
        sys.exit(
            f"[gfs]  Could not find any available GFS 0.125° index for "
            f"step=+{step}h after trying all recent cycles. "
            "NOMADS may be temporarily unavailable, or the step is too large "
            "for the 0.125° product."
        )

    def find_byte_range(search: str) -> tuple[int, int | None]:
        for i, line in enumerate(idx_lines):
            if search in line:
                parts = line.split(":")
                start = int(parts[1])
                end: int | None = None
                if i + 1 < len(idx_lines):
                    next_parts = idx_lines[i + 1].split(":")
                    end = int(next_parts[1]) - 1
                return start, end
        sys.exit(
            f"Could not find '{search}' in GFS index.\n"
            "The level may not be available in this GFS file. "
            "Check available levels with a pressure level like 500, 850, 250."
        )

    def download_range(search: str, out_path: Path) -> None:
        start, end = find_byte_range(search)
        range_header = f"bytes={start}-{end}" if end is not None else f"bytes={start}-"
        print(f"[gfs]  Downloading '{search}'  ({range_header}) …")
        resp = requests.get(
            base_url,
            headers={"Range": range_header},
            timeout=120,
            stream=True,
        )
        resp.raise_for_status()
        out_path.write_bytes(resp.content)

    u_path = out_dir / "u.grib2"
    v_path = out_dir / "v.grib2"
    download_range(u_search, u_path)
    download_range(v_search, v_path)
    return u_path, v_path


# ---------------------------------------------------------------------------
# GRIB → numpy array (lat × lon, regular grid)
# ---------------------------------------------------------------------------

def grib_to_array(grib_path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Read a single-field GRIB2 file and return (data, lats, lons).
    lats:  1-D, descending (90 … −90)
    lons:  1-D, ascending  (0 … 359.75 or −180 … 180)
    data:  2-D float32, shape (nlat, nlon)
    """
    try:
        import cfgrib
        import xarray as xr
    except ImportError:
        sys.exit(
            "cfgrib / xarray are not installed.\n"
            "Run:  pip install cfgrib xarray\n"
            "You also need the eccodes C library:  conda install -c conda-forge eccodes"
        )

    ds = xr.open_dataset(str(grib_path), engine="cfgrib")
    var = list(ds.data_vars)[0]          # only one variable in each file
    da = ds[var]

    lats = da.latitude.values.astype(np.float32)
    lons = da.longitude.values.astype(np.float32)
    data = da.values.astype(np.float32)

    # Ensure lats are descending (north→south) as expected by the renderer
    if lats[0] < lats[-1]:
        lats = lats[::-1]
        data = data[::-1, :]

    # Normalise lons to [−180, 180) if they arrive as [0, 360)
    if lons.max() > 180.0:
        shift = np.searchsorted(lons, 180.0)
        lons = np.concatenate([lons[shift:] - 360.0, lons[:shift]])
        data = np.concatenate([data[:, shift:], data[:, :shift]], axis=1)

    return data, lats, lons


# ---------------------------------------------------------------------------
# Bilinear resample to target grid
# ---------------------------------------------------------------------------

def resample(
    data: np.ndarray,
    src_lats: np.ndarray,
    src_lons: np.ndarray,
    dst_height: int,
    dst_width: int,
) -> np.ndarray:
    """
    Bilinearly resample *data* (nlat × nlon) onto a (dst_height × dst_width)
    equirectangular grid covering [90, −90] × [−180, 180].
    """
    from scipy.ndimage import map_coordinates

    # Target grid pixel centres
    dst_lats = np.linspace(90.0, -90.0, dst_height, dtype=np.float32)
    dst_lons = np.linspace(-180.0, 180.0, dst_width,  dtype=np.float32, endpoint=False)

    # Map target lat/lon to fractional source indices
    lat_idx = np.interp(dst_lats, src_lats[::-1], np.arange(len(src_lats))[::-1])
    lon_idx = np.interp(dst_lons, src_lons,        np.arange(len(src_lons)))

    # Build coordinate arrays for map_coordinates  (shape: 2 × H × W)
    grid_lat, grid_lon = np.meshgrid(lat_idx, lon_idx, indexing="ij")
    coords = np.array([grid_lat, grid_lon])

    return map_coordinates(data, coords, order=1, mode="wrap").astype(np.float32)


# ---------------------------------------------------------------------------
# Encode UV → RGB
# ---------------------------------------------------------------------------

def encode_to_rgb(
    u: np.ndarray,
    v: np.ndarray,
    max_uv: float,
    max_speed: float,
) -> np.ndarray:
    """
    u, v      – 2-D arrays, same shape, in m/s
    max_uv    – clamp range for U/V before mapping to [0, 255]  (128 = 0 m/s)
    max_speed – wind speed that maps to B = 255

    Returns uint8 array of shape (H, W, 3).
    """
    # --- R: U component  ---
    # 0 m/s → 128,  +max_uv → 255,  −max_uv → 0
    r = np.clip(u, -max_uv, max_uv)
    r = ((r + max_uv) / (2.0 * max_uv) * 255.0).astype(np.uint8)

    # --- G: V component  ---
    g = np.clip(v, -max_uv, max_uv)
    g = ((g + max_uv) / (2.0 * max_uv) * 255.0).astype(np.uint8)

    # --- B: wind speed magnitude ---
    speed = np.sqrt(u ** 2 + v ** 2)
    b = np.clip(speed / max_speed * 255.0, 0, 255).astype(np.uint8)

    return np.stack([r, g, b], axis=-1)   # (H, W, 3)


# ---------------------------------------------------------------------------
# Save PNG with metadata
# ---------------------------------------------------------------------------

def save_png(rgb: np.ndarray, out_path: str, meta: dict) -> None:
    try:
        from PIL import Image, PngImagePlugin
    except ImportError:
        sys.exit("Pillow is not installed.\nRun:  pip install pillow")

    img = Image.fromarray(rgb, mode="RGB")

    # Embed encoding metadata in the PNG text chunks
    pnginfo = PngImagePlugin.PngInfo()
    for k, v in meta.items():
        pnginfo.add_text(k, str(v))

    img.save(out_path, format="PNG", pnginfo=pnginfo)
    print(f"[done] Saved {out_path}  ({img.width}×{img.height} px)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()

    with tempfile.TemporaryDirectory(prefix="ecmwf_wind_") as tmp:
        tmp_dir = Path(tmp)

        # 1. Download
        if args.model == "gfs":
            u_grib, v_grib = fetch_wind_grib_gfs(args.step, args.level, tmp_dir)
        else:
            u_grib, v_grib = fetch_wind_grib(args.step, args.level, tmp_dir)

        # 2. Decode GRIB
        print("[decode] Reading U component …")
        u_raw, lats, lons = grib_to_array(u_grib)

        print("[decode] Reading V component …")
        v_raw, _, _ = grib_to_array(v_grib)

        print(f"[grid]   Source grid: {u_raw.shape[0]} lat × {u_raw.shape[1]} lon")
        print(f"         lat range : {lats[0]:.2f} … {lats[-1]:.2f}")
        print(f"         lon range : {lons[0]:.2f} … {lons[-1]:.2f}")
        print(f"         U  min/max: {u_raw.min():.1f} / {u_raw.max():.1f} m/s")
        print(f"         V  min/max: {v_raw.min():.1f} / {v_raw.max():.1f} m/s")

        # 3. Resample to target resolution
        print(f"[resamp] Resampling to {args.width}×{args.height} …")
        u = resample(u_raw, lats, lons, args.height, args.width)
        v = resample(v_raw, lats, lons, args.height, args.width)

        # 4. Encode
        print("[encode] Building RGB channels …")
        rgb = encode_to_rgb(u, v, max_uv=args.max_uv, max_speed=args.max_speed)

    # 5. Save
    model_label = "ECMWF HRES" if args.model == "ecmwf" else "NOAA GFS 0.125°"
    meta = {
        "Description":  f"{model_label} global wind at {args.level} – UV encoded in RGB",
        "Model":         args.model.upper(),
        "Encoding":      "R=U_component, G=V_component, B=wind_speed",
        "R_formula":     f"clamp(U, ±{args.max_uv}) mapped to [0,255]; 128=0 m/s",
        "G_formula":     f"clamp(V, ±{args.max_uv}) mapped to [0,255]; 128=0 m/s",
        "B_formula":     f"sqrt(U²+V²) / {args.max_speed} * 255; clipped at 255",
        "Projection":    "Equirectangular, top-left=(90N,180W), bottom-right=(90S,180E)",
        "Level":          args.level,
        "ForecastStep":  f"T+{args.step}h",
        "MaxUV_ms":      str(args.max_uv),
        "MaxSpeed_ms":   str(args.max_speed),
    }
    save_png(rgb, args.output, meta)

    # Print decoding formulas for downstream consumers
    print()
    print("Decoding formulas (pixel value p ∈ [0, 255]):")
    print(f"  U (m/s) = (R / 255 * 2 - 1) * {args.max_uv}")
    print(f"  V (m/s) = (G / 255 * 2 - 1) * {args.max_uv}")
    print(f"  speed   = B / 255 * {args.max_speed}")


if __name__ == "__main__":
    main()
