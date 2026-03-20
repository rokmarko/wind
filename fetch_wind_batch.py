#!/usr/bin/env python3
"""
fetch_wind_batch.py
-------------------
Download and convert wind forecasts for multiple levels and the next 48 hours,
saving UV-encoded PNGs into <model-dir>/<model_name>/.

Models : gfs   – NOAA GFS 0.25° (NOMADS, default)
         ecmwf – ECMWF Open Data HRES (~0.1°)

Levels (GFS)  : 10m, 1000, 975, 950, 925, 900, 850, 800, 750, 700, 650, 600, 500 hPa
Levels (ECMWF): 10m, 1000, 925, 850, 700, 600, 500 hPa  (limited open-data set)
Steps  : 0, 3, 6 … 48 h  (every 3 h)
Output : <model-dir>/<model>/wind_{level}_{YYYYMMDD_HH}.png
  e.g.  model/gfs/wind_850hpa_20260320_06.png
        model/ecmwf/wind_10m_20260320_00.png

For GFS, only one .idx file is fetched per forecast step; all 8 levels are
pulled via byte-range requests.  Existing PNGs are skipped only when their
embedded 'Run' metadata matches the current model cycle; a newer cycle will
overwrite them.  This makes re-runs both safe and up-to-date.

Usage
-----
  python fetch_wind_batch.py [--model gfs|ecmwf] [--model-dir model]
                             [--max-uv 50] [--max-speed 50]
                             [--width 2048] [--height 1024]

Dependencies (same venv as ecmwf_wind_map.py)
  pip install cfgrib xarray scipy pillow numpy requests ecmwf-opendata
"""

import argparse
import datetime
import os
import sys
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# GFS provides a dense set of pressure levels including 950/900/800/600 hPa.
# ECMWF Open Data publishes a much sparser mandatory-level set.
# GFS_LEVELS:   list[str] = ["10m", "1000", "975", "950", "925", "900", "850", "800", "750", "700", "650", "600", "500"]
GFS_LEVELS:   list[str] = ["10m", "1000", "925", "850", "700", "600", "500"]
ECMWF_LEVELS: list[str] = ["10m", "1000", "925", "850", "700", "600", "500"]
STEPS:  list[int] = list(range(0, 25, 3))   # 0, 3, 6, … 24

# Defaults (may be overridden by CLI args)
MAX_UV    = 50.0
MAX_SPEED = 50.0
WIDTH     = 2048
HEIGHT    = 1024

# ---------------------------------------------------------------------------
# Import helpers from ecmwf_wind_map (same directory)
# ---------------------------------------------------------------------------

try:
    from ecmwf_wind_map import (
        _gfs_cycle_candidates,
        _gfs_idx_search_strings,
        _parse_level,
        fetch_wind_grib,
        grib_to_array,
        resample,
        encode_to_rgb,
        save_png,
    )
except ImportError as exc:
    sys.exit(
        f"Cannot import from ecmwf_wind_map.py: {exc}\n"
        "Make sure ecmwf_wind_map.py is in the same directory and dependencies are installed."
    )

# ---------------------------------------------------------------------------
# GFS cycle resolution
# ---------------------------------------------------------------------------

def resolve_gfs_cycle(step: int) -> tuple[str, str]:
    """
    Walk candidate GFS cycles (most-recent first) and return the first one
    where the .idx file for the given step actually exists on NOMADS.
    Uses a lightweight HEAD request to avoid downloading the index.
    """
    import requests

    step_str = f"{step:03d}"
    for date_str, cycle in _gfs_cycle_candidates():
        idx_url = (
            f"https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/prod/"
            f"gfs.{date_str}/{cycle}/atmos/"
            f"gfs.t{cycle}z.pgrb2.0p25.f{step_str}.idx"
        )
        try:
            r = requests.head(idx_url, timeout=15)
            if r.status_code == 200:
                return date_str, cycle
            if r.status_code != 404:
                r.raise_for_status()
        except requests.RequestException:
            continue
    sys.exit(
        f"[batch] No available GFS 0.25° run found for step=+{step}h. "
        "NOMADS may be temporarily unavailable."
    )

# ---------------------------------------------------------------------------
# ECMWF: resolve run info (for labelling) + per-step download
# ---------------------------------------------------------------------------

def resolve_ecmwf_run() -> datetime.datetime:
    """
    Return the datetime of the latest available ECMWF run.
    Falls back to the most-recent 00/06/12/18Z boundary if the query fails.
    """
    try:
        from ecmwf.opendata import Client
        client = Client(source="ecmwf")
        latest = client.latest(type="fc", param="10u", step=0)
        # latest is already a datetime
        return latest
    except Exception:
        pass
    # Fallback: last 6-hourly boundary at least 5 h ago
    now = datetime.datetime.utcnow()
    for h in [18, 12, 6, 0]:
        dt = now.replace(hour=h, minute=0, second=0, microsecond=0)
        if (now - dt).total_seconds() >= 5 * 3600:
            return dt
    return now.replace(hour=0, minute=0, second=0, microsecond=0) - datetime.timedelta(days=1)


def fetch_step_gribs_ecmwf(
    step: int, tmp_dir: Path, levels: list[str]
) -> dict[str, tuple[Path, Path]]:
    """
    Download U+V GRIB files for the given levels from ECMWF Open Data.
    Returns level_str → (u_path, v_path).

    TQDM_DISABLE suppresses the per-file tqdm progress bars emitted by the
    underlying multiurl library for every catalogue/index fetch.
    """
    try:
        from ecmwf.opendata import Client
    except ImportError:
        sys.exit("ecmwf-opendata is not installed.\nRun:  pip install ecmwf-opendata")

    client = Client(source="ecmwf")
    results: dict[str, tuple[Path, Path]] = {}

    # Suppress tqdm progress bars for all transfers in this function
    _prev = os.environ.get("TQDM_DISABLE")
    os.environ["TQDM_DISABLE"] = "1"
    try:
        for level in levels:
            u_param, v_param, pressure = _parse_level(level)
            lv_dir = tmp_dir / level
            lv_dir.mkdir(exist_ok=True)
            u_path = lv_dir / "u.grib2"
            v_path = lv_dir / "v.grib2"
            base_kwargs: dict = {"type": "fc", "step": step}
            if pressure is not None:
                base_kwargs["levtype"] = "pl"
                base_kwargs["levelist"] = pressure
            try:
                print(f"  [ecmwf] {u_param}/{v_param}"
                      f"{'@'+str(pressure)+'hPa' if pressure else ''} step={step}h …")
                client.retrieve(**base_kwargs, param=u_param, target=str(u_path))
                client.retrieve(**base_kwargs, param=v_param, target=str(v_path))
                results[level] = (u_path, v_path)
            except Exception as exc:
                print(f"  [warn] ECMWF level={level} step={step}: {exc}")
    finally:
        if _prev is None:
            os.environ.pop("TQDM_DISABLE", None)
        else:
            os.environ["TQDM_DISABLE"] = _prev

    return results


# ---------------------------------------------------------------------------
# GFS: per-step fetch index + download all levels
# ---------------------------------------------------------------------------

def fetch_step_gribs(
    date_str: str, cycle: str, step: int, tmp_dir: Path
) -> dict[str, tuple[Path, Path]]:
    """
    Fetch the .idx for this (run, step), then for each level download the U
    and V records via byte-range requests.

    Returns a dict  level_str → (u_grib_path, v_grib_path)
    for levels that were successfully downloaded.
    """
    import requests

    step_str = f"{step:03d}"
    base_url = (
        f"https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/prod/"
        f"gfs.{date_str}/{cycle}/atmos/"
        f"gfs.t{cycle}z.pgrb2.0p25.f{step_str}"
    )
    idx_url = base_url + ".idx"
    print(f"  [idx]  {idx_url}")
    r = requests.get(idx_url, timeout=30)
    r.raise_for_status()
    idx_lines = r.text.splitlines()

    def find_byte_range(search: str) -> tuple[int, int | None] | None:
        for i, line in enumerate(idx_lines):
            if search in line:
                parts = line.split(":")
                start = int(parts[1])
                end: int | None = None
                if i + 1 < len(idx_lines):
                    end = int(idx_lines[i + 1].split(":")[1]) - 1
                return start, end
        return None

    def download_range(search: str, out_path: Path) -> bool:
        result = find_byte_range(search)
        if result is None:
            print(f"  [warn] '{search}' not found in index – skipping")
            return False
        start, end = result
        range_header = f"bytes={start}-{end}" if end is not None else f"bytes={start}-"
        resp = requests.get(
            base_url,
            headers={"Range": range_header},
            timeout=120,
            stream=True,
        )
        resp.raise_for_status()
        out_path.write_bytes(resp.content)
        return True

    results: dict[str, tuple[Path, Path]] = {}
    for level in GFS_LEVELS:
        u_search, v_search = _gfs_idx_search_strings(level)
        u_path = tmp_dir / f"u_{level}_f{step_str}.grib2"
        v_path = tmp_dir / f"v_{level}_f{step_str}.grib2"
        if download_range(u_search, u_path) and download_range(v_search, v_path):
            results[level] = (u_path, v_path)
    return results

# ---------------------------------------------------------------------------
# Level label helpers
# ---------------------------------------------------------------------------

def level_label(level: str) -> str:
    """'10m' → '10m',  '500' → '500hpa'"""
    return "10m" if level.lower() == "10m" else f"{level}hpa"

def out_path_for(level: str, valid_dt: datetime.datetime, out_dir: Path) -> Path:
    vts = valid_dt.strftime("%Y%m%d_%H")
    return out_dir / f"wind_{level_label(level)}_{vts}.png"


def png_run_dt(path: Path) -> datetime.datetime | None:
    """
    Read the 'Run' PNG text chunk (e.g. '20260320 T06Z') from an existing
    output file and return it as a datetime, or None if unreadable.
    """
    try:
        from PIL import Image
        with Image.open(path) as img:
            run_str = img.text.get("Run", "")  # type: ignore[attr-defined]
        return datetime.datetime.strptime(run_str, "%Y%m%d T%HZ")
    except Exception:
        return None


def needs_update(path: Path, run_dt: datetime.datetime) -> bool:
    """True when the file does not exist or was produced by an older run."""
    if not path.exists():
        return True
    stored = png_run_dt(path)
    return stored is None or run_dt > stored


# ---------------------------------------------------------------------------
# Process one forecast step (model-agnostic)
# ---------------------------------------------------------------------------

def process_step(
    model: str,
    run_dt: datetime.datetime,
    date_str: str, cycle: str,   # used by GFS only; ignored for ECMWF
    step: int,
    levels: list[str],
    out_dir: Path, max_uv: float, max_speed: float,
    width: int, height: int,
) -> None:
    valid_dt = run_dt + datetime.timedelta(hours=step)
    run_label = run_dt.strftime("%Y%m%d T%HZ")
    print(f"\n[step +{step:02d}h]  {run_label}  →  valid {valid_dt.strftime('%Y%m%d %HZ')}")

    missing = [lv for lv in levels if needs_update(out_path_for(lv, valid_dt, out_dir), run_dt)]
    if not missing:
        print("  All levels up-to-date – skipped.")
        return

    prefix = "gfs_batch_" if model == "gfs" else "ecmwf_batch_"
    with tempfile.TemporaryDirectory(prefix=prefix) as tmp:
        tmp_dir = Path(tmp)
        if model == "gfs":
            grib_map = fetch_step_gribs(date_str, cycle, step, tmp_dir)
            model_label = "GFS 0.25°"
        else:
            grib_map = fetch_step_gribs_ecmwf(step, tmp_dir, levels)
            model_label = "ECMWF HRES"

        for level, (u_grib, v_grib) in grib_map.items():
            out = out_path_for(level, valid_dt, out_dir)
            if not needs_update(out, run_dt):
                print(f"  [skip] {out.name}")
                continue

            label = level_label(level)
            print(f"  [conv] {label} …", end=" ", flush=True)
            try:
                u_raw, lats, lons = grib_to_array(u_grib)
                v_raw, _,    _    = grib_to_array(v_grib)
                u = resample(u_raw, lats, lons, height, width)
                v = resample(v_raw, lats, lons, height, width)
                rgb = encode_to_rgb(u, v, max_uv=max_uv, max_speed=max_speed)
            except Exception as exc:
                print(f"ERROR  {exc}")
                continue

            meta = {
                "Model":        model_label,
                "Run":          run_label,
                "ValidTime":    valid_dt.strftime("%Y%m%d %HZ"),
                "ForecastStep": f"T+{step}h",
                "Level":        level,
                "Encoding":     "R=U_component, G=V_component, B=wind_speed",
                "R_formula":    f"clamp(U, ±{max_uv}) → [0,255]; 128=0 m/s",
                "G_formula":    f"clamp(V, ±{max_uv}) → [0,255]; 128=0 m/s",
                "B_formula":    f"sqrt(U²+V²) / {max_speed} * 255",
                "Projection":   "Equirectangular, top-left=(90N,180W), bottom-right=(90S,180E)",
            }
            save_png(rgb, str(out), meta)

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Batch-download wind forecast – 8 levels × 48 h → model/<model>/"
    )
    p.add_argument(
        "--model", default="gfs", choices=["gfs", "ecmwf"],
        help="Forecast model: 'gfs' = NOAA GFS 0.25° (default), 'ecmwf' = ECMWF HRES",
    )
    p.add_argument(
        "--list-levels", action="store_true",
        help="Print the available pressure levels for each model and exit",
    )
    p.add_argument("--model-dir",  default="model",      help="Base output directory (default: model)")
    p.add_argument("--max-uv",     type=float, default=MAX_UV,
                   help=f"±max m/s for U/V normalisation (default: {MAX_UV})")
    p.add_argument("--max-speed",  type=float, default=MAX_SPEED,
                   help=f"Speed mapped to B=255 (default: {MAX_SPEED})")
    p.add_argument("--width",      type=int,   default=WIDTH,
                   help=f"Output image width  (default: {WIDTH})")
    p.add_argument("--height",     type=int,   default=HEIGHT,
                   help=f"Output image height (default: {HEIGHT})")
    return p.parse_args()

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _list_gfs_levels_live() -> list[str] | None:
    """
    Fetch the NOMADS GFS idx for step=0 from the latest available cycle and
    return a sorted list of UGRD level strings (e.g. '500 mb', '10 m above ground').
    Returns None if the request fails.
    """
    import requests

    for date_str, cycle in _gfs_cycle_candidates():
        idx_url = (
            f"https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/prod/"
            f"gfs.{date_str}/{cycle}/atmos/gfs.t{cycle}z.pgrb2.0p25.f000.idx"
        )
        try:
            r = requests.get(idx_url, timeout=20)
            if r.status_code == 200:
                levels: list[str] = []
                for line in r.text.splitlines():
                    parts = line.split(":")
                    if len(parts) >= 5 and parts[3] == "UGRD":
                        lv = parts[4]
                        if lv not in levels:
                            levels.append(lv)
                return levels
            if r.status_code not in (403, 404):
                r.raise_for_status()
        except requests.RequestException:
            continue
    return None


def _list_ecmwf_levels_live() -> tuple[list[int], bool] | None:
    """
    Fetch the ECMWF scda index for step=0 from the latest available run and
    return (sorted list of pressure levels in hPa, has_10m_wind).
    Returns None if the request fails.
    """
    import json
    import requests

    try:
        from ecmwf.opendata import Client
        client = Client(source="ecmwf")
        # Derive the base URL used by the client
        base_url = client.url.rstrip("/")
    except Exception:
        base_url = "https://data.ecmwf.int/forecasts"

    run_dt = resolve_ecmwf_run()
    date_s = run_dt.strftime("%Y%m%d")
    hour_s = run_dt.strftime("%H")
    ts_s   = run_dt.strftime("%Y%m%d%H%M%S")
    index_url = (
        f"{base_url}/{date_s}/{hour_s}z/ifs/0p25/scda/"
        f"{ts_s}-0h-scda-fc.index"
    )
    try:
        r = requests.get(index_url, timeout=20)
        if r.status_code != 200:
            r.raise_for_status()
    except requests.RequestException:
        return None

    pl_levels: set[int] = set()
    sfc_params: set[str] = set()
    for line in r.text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("levtype") == "pl":
            try:
                pl_levels.add(int(entry["levelist"]))
            except (KeyError, ValueError):
                pass
        elif entry.get("levtype") == "sfc":
            sfc_params.add(str(entry.get("param", "")))

    has_10m = "10u" in sfc_params or "10v" in sfc_params
    return sorted(pl_levels, reverse=True), has_10m


def main() -> None:
    args = parse_args()

    if args.list_levels:
        print("Querying live model sources for available wind levels …\n")

        # ── GFS ──
        print("GFS 0.25° (NOMADS):")
        gfs_live = _list_gfs_levels_live()
        if gfs_live is not None:
            mb_levels = [lv for lv in gfs_live if lv.endswith(" mb")]
            surface   = [lv for lv in gfs_live if "above ground" in lv]
            print(f"  Pressure levels : {', '.join(mb_levels)}")
            print(f"  Surface         : {', '.join(surface)}")
            print(f"  (batch defaults : {', '.join(GFS_LEVELS)})")
        else:
            print("  [warn] Could not reach NOMADS – falling back to hardcoded defaults:")
            print(f"  {', '.join(GFS_LEVELS)}")
        print()

        # ── ECMWF ──
        print("ECMWF HRES Open Data:")
        ecmwf_live = _list_ecmwf_levels_live()
        if ecmwf_live is not None:
            pl_levels, has_10m = ecmwf_live
            pl_str = ", ".join(f"{lv} hPa" for lv in pl_levels)
            sfc_str = "10 m wind (10u/10v)" if has_10m else "no 10 m wind found"
            print(f"  Pressure levels : {pl_str}")
            print(f"  Surface         : {sfc_str}")
            print(f"  (batch defaults : {', '.join(ECMWF_LEVELS)})")
        else:
            print("  [warn] Could not reach ECMWF data server – falling back to hardcoded defaults:")
            print(f"  {', '.join(ECMWF_LEVELS)}")

        sys.exit(0)

    out_dir = Path(args.model_dir) / args.model
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.model == "gfs":
        try:
            import requests  # noqa: F401
        except ImportError:
            sys.exit("requests is not installed.\nRun:  pip install requests")

    model_title = "GFS 0.25°" if args.model == "gfs" else "ECMWF HRES"
    levels = GFS_LEVELS if args.model == "gfs" else ECMWF_LEVELS
    total = len(levels) * len(STEPS)
    print(f"[batch] {model_title}  –  {len(levels)} levels × {len(STEPS)} steps = {total} PNGs")
    print(f"[batch] Levels : {', '.join(levels)}")
    print(f"[batch] Steps  : {', '.join(str(s) for s in STEPS)} h")
    print(f"[batch] Output : {out_dir.resolve()}")
    print()

    if args.model == "gfs":
        print("[batch] Resolving latest available GFS cycle …")
        date_str, cycle = resolve_gfs_cycle(STEPS[0])
        run_dt = datetime.datetime.strptime(f"{date_str}{cycle}", "%Y%m%d%H")
        print(f"[batch] Using run : {run_dt.strftime('%Y%m%d T%HZ')}\n")
    else:
        date_str, cycle = "", ""
        print("[batch] Resolving latest ECMWF run …")
        run_dt = resolve_ecmwf_run()
        print(f"[batch] Using run : {run_dt.strftime('%Y%m%d T%HZ')}\n")

    for step in STEPS:
        try:
            process_step(
                args.model, run_dt, date_str, cycle,
                step, levels, out_dir,
                args.max_uv, args.max_speed, args.width, args.height,
            )
        except Exception as exc:
            print(f"[error] step +{step:02d}h failed: {exc}")

    pngs = sorted(out_dir.glob("wind_*.png"))
    print(f"\n[batch] Finished.  {len(pngs)} PNGs in {out_dir.resolve()}")
    for p in pngs:
        print(f"  {p.name}")


if __name__ == "__main__":
    main()
