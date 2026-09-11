#!/usr/bin/env python3
"""
nesis_radar_png.py
------------------
Converts an OPERA radar PNG produced by opera_radar_map.py into the fixed
"EU weather image" texture that Nesis expects.

Nesis uploads the PNG as a single GL texture stretched over a hard-coded
bounding box, so the image must be reprojected onto exactly the grid the
renderer assumes.  The requirements below are not conventions — they are read
straight out of the Nesis sources:

  Projection   Spherical Mercator (EPSG:3857).  OGLSphereTriangulator.cpp
               Triangulate2() computes the vertical texture coordinate as
                   fY = log(tan(M_PI_4 + fLat/2));  v = (fY - fTS)/fLD
               That is the *spherical* Mercator formula with no eccentricity
               term, so EPSG:3395 (ellipsoidal) is wrong by ~40 km at 70°N.

  Extent       WeatherImageRenderer.cpp:57-58
                   W −14.618225054687514°   E  45.314636273437486°
                   S  30.968189526345665°   N  72.62025190354672°
               = -1627293.369 … 5044402.235 x, 3628618.637 … 11980434.095 y
               in EPSG:3857 metres.

  Orientation  Row 0 of the PNG is the NORTH edge.  Triangulate2 puts v=0 at
               the south, and WeatherImageRenderer::Update() calls
               QImage::mirrored() before uploading, which flips it for GL's
               bottom-left origin.

  Pixel grid   u is linear in longitude, v is linear in Mercator y.  GL texture
               coordinates 0 and 1 sit on the texture's outer *edges*, so the
               bbox corners land on pixel edges and pixel centres are at
               (i+0.5)/W — no half-pixel offset.

  Format       8-bit RGBA, straight (non-premultiplied) alpha.  Nesis converts
               to QImage::Format_RGBA8888 (the non-premultiplied one) and
               Weather.fsh does vec4(color.rgb, color.a*glf_alpha), i.e. classic
               SRC_ALPHA / ONE_MINUS_SRC_ALPHA blending.  A premultiplied image
               would show dark fringes.

  Size         Free — u/v are normalised, so any W×H is stretched onto the bbox.
               Defaults to 2048×2048.  The bbox aspect is w/h = 0.798832, so a
               square texture resolves 25% less finely in the vertical; pass
               --isotropic for a matched 2048×2565 instead.

Resampling
----------
The source is a 1 km grid; the target is typically ~3-4× coarser on the ground,
so plain nearest-neighbour would drop small cells and make thin lines shimmer.
Each output pixel therefore takes the *strongest* of N×N sub-samples
(--supersample), which is how radar products are normally reduced.  Sub-samples
are ranked by their position along the source's own colour ramp, read from the
Colormap text chunk that opera_radar_map.py embeds.

Only whole source pixels are ever copied — no colour is ever blended — so the
output contains exactly the ramp colours of the input, no invented intermediate
hues bleeding out of transparent areas, and no premultiplication.

Usage
-----
  pip install pyproj pillow numpy
  python opera_radar_map.py --output opera_dbzh.png
  python nesis_radar_png.py opera_dbzh.png [--output nesis_weather.png]
                            [--width 2048] [--height H | --isotropic]
                            [--supersample 3]

Dependencies
------------
  pyproj  – EPSG:3857 and the composite's LAEA grid
  Pillow  – PNG in/out
  numpy   – everything numeric
"""

import argparse
import re
import sys
from pathlib import Path

import numpy as np

# ---------------------------------------------------------------------------
# The target grid, straight from WeatherImageRenderer.cpp:57-58
# ---------------------------------------------------------------------------

NESIS_WEST = -14.618225054687514
NESIS_EAST = 45.314636273437486
NESIS_SOUTH = 30.968189526345665
NESIS_NORTH = 72.62025190354672

# Natural aspect of the bbox in EPSG:3857 metres.  The texture is stretched onto
# the bbox regardless, so this only decides how detail is split between the axes:
# a square texture spends 25% less resolution vertically than horizontally.
NESIS_ASPECT = 0.798832

# Default texture size.  Square and power-of-two for the widest GPU compatibility.
DEFAULT_SIZE = 2048

WEBMERCATOR = "EPSG:3857"
WGS84 = "EPSG:4326"


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="OPERA radar PNG → Nesis EU weather image (EPSG:3857)"
    )
    p.add_argument(
        "input", type=str,
        help="Input PNG from opera_radar_map.py (carries its projection in the "
             "PNG text chunks)"
    )
    p.add_argument(
        "--output", type=str, default="nesis_weather.png",
        help="Output PNG file path (default: nesis_weather.png)"
    )
    p.add_argument(
        "--width", type=int, default=DEFAULT_SIZE,
        help=f"Output width in pixels (default: {DEFAULT_SIZE})"
    )
    p.add_argument(
        "--height", type=int, default=None,
        help="Output height in pixels (default: same as --width, i.e. square)"
    )
    p.add_argument(
        "--isotropic", action="store_true",
        help=(
            f"Set the height to width / {NESIS_ASPECT} so the output resolves "
            "equally in both axes. A square texture is stretched onto a bbox "
            "that is not square, so it resolves 25% less finely vertically."
        ),
    )
    p.add_argument(
        "--supersample", type=int, default=3,
        help=(
            "Take the strongest of N×N sub-samples per output pixel "
            "(default: 3). Use 1 for plain nearest-neighbour."
        ),
    )
    p.add_argument(
        "--no-bleed", action="store_true",
        help=(
            "Skip colour dilation into transparent pixels. Nesis filters the "
            "texture with GL_LINEAR, so without bleeding the echo edges pick up "
            "a halo from whatever RGB the transparent texels carry."
        ),
    )
    return p.parse_args()


# ---------------------------------------------------------------------------
# Source georeferencing, read from the input PNG's text chunks
# ---------------------------------------------------------------------------

def read_source(path: str) -> tuple[np.ndarray, dict]:
    """
    Load the source PNG as RGBA uint8 and pull its georeferencing out of the
    PNG text chunks written by opera_radar_map.py.
    """
    try:
        from PIL import Image
    except ImportError:
        sys.exit("Pillow is not installed.\nRun:  pip install pillow")

    img = Image.open(path)
    text = dict(img.text)

    missing = [k for k in ("Projection", "Corner_UL", "PixelSize_m") if k not in text]
    if missing:
        sys.exit(
            f"{path} is missing the PNG metadata {', '.join(missing)}.\n"
            "This converter needs an image written by opera_radar_map.py, which "
            "embeds the composite's projection and grid origin."
        )

    rgba = np.asarray(img.convert("RGBA"))
    ul_lat, ul_lon = (float(v) for v in text["Corner_UL"].split(","))

    meta = {
        "projdef":   text["Projection"],
        "ul_lat":    ul_lat,
        "ul_lon":    ul_lon,
        "pixel_m":   float(text["PixelSize_m"]),
        "colormap":  text.get("Colormap", ""),
        "text":      text,
    }
    return rgba, meta


def source_origin(meta: dict) -> tuple[float, float]:
    """
    Project the composite's upper-left corner into its own LAEA grid.

    The ODIM /where corner lat/lons are the *outer edges* of the corner pixels:
    projecting all four lands on an exact xsize*xscale × ysize*yscale box.  (The
    publisher's own GeoTIFF declares an origin half a pixel away from this; the
    ODIM reading is the self-consistent one, and half a pixel is 500 m against
    an output pixel of several kilometres.)
    """
    from pyproj import CRS, Transformer

    to_laea = Transformer.from_crs(WGS84, CRS.from_proj4(meta["projdef"]), always_xy=True)
    return to_laea.transform(meta["ul_lon"], meta["ul_lat"])


# ---------------------------------------------------------------------------
# Ranking colours along the source's own ramp
# ---------------------------------------------------------------------------

def _pack(rgb: np.ndarray) -> np.ndarray:
    """Pack an (..., 3) uint8 array into one uint32 per pixel."""
    rgb = rgb.astype(np.uint32)
    return (rgb[..., 0] << 16) | (rgb[..., 1] << 8) | rgb[..., 2]


def build_rank_lut(colormap: str) -> tuple[np.ndarray, np.ndarray]:
    """
    Rebuild the source's colour ramp from the Colormap text chunk
    ("5dBZ=#04e9e7 10dBZ=#019ff4 …") and return (sorted_packed, rank) so that a
    pixel colour can be ranked by how far along the ramp it sits.

    The source generated its colours by interpolating this ramp at values on a
    0.5 dBZ grid, so sampling the ramp finely reproduces every colour it can
    contain and the lookup is an exact match, not a nearest-colour search.
    """
    stops = re.findall(r"(-?[\d.]+)dBZ=#([0-9a-fA-F]{6})", colormap)
    if len(stops) < 2:
        try:
            from opera_radar_map import DBZ_COLOUR_STOPS
        except ImportError:
            return np.empty(0, np.uint32), np.empty(0, np.int32)
        levels = np.array([s for s, _ in DBZ_COLOUR_STOPS], float)
        colours = np.array([c for _, c in DBZ_COLOUR_STOPS], float)
    else:
        levels = np.array([float(s) for s, _ in stops], float)
        colours = np.array(
            [[int(h[i:i + 2], 16) for i in (0, 2, 4)] for _, h in stops], float
        )

    # Sample well past both ends of the ramp; np.interp clamps, matching the
    # source, so the out-of-range samples simply repeat the end colours.
    probe = np.arange(levels[0] - 40.0, levels[-1] + 10.0, 0.25)
    rgb = np.stack(
        [np.interp(probe, levels, colours[:, c]).astype(np.uint8) for c in range(3)],
        axis=-1,
    )
    packed = _pack(rgb)

    # Keep the first (lowest-dBZ) occurrence of each colour; rank is that index.
    packed, first = np.unique(packed, return_index=True)
    return packed, first.astype(np.int32)


def rank_of(rgb: np.ndarray, packed: np.ndarray, ranks: np.ndarray) -> np.ndarray:
    """
    Rank each pixel colour along the ramp.  Colours that are not ramp colours —
    the --show-coverage tint, for instance — rank below every echo colour.
    """
    if packed.size == 0:
        return np.zeros(rgb.shape[:-1], np.int32)
    key = _pack(rgb)
    idx = np.searchsorted(packed, key)
    idx_clipped = np.clip(idx, 0, packed.size - 1)
    hit = packed[idx_clipped] == key
    return np.where(hit, ranks[idx_clipped], -1).astype(np.int32)


# ---------------------------------------------------------------------------
# Reprojection
# ---------------------------------------------------------------------------

def reproject(
    src: np.ndarray,
    meta: dict,
    width: int,
    height: int,
    supersample: int,
) -> np.ndarray:
    """
    Resample the source composite onto the Nesis EPSG:3857 grid.

    Columns are equally spaced in longitude and rows equally spaced in Mercator
    y, with pixel centres at (i+0.5)/W and (j+0.5)/H so the bbox corners fall on
    pixel edges.  Row 0 is the north edge.
    """
    try:
        from pyproj import CRS, Transformer
    except ImportError:
        sys.exit("pyproj is not installed.\nRun:  pip install pyproj")

    src_h, src_w = src.shape[:2]
    px = meta["pixel_m"]
    x_ul, y_ul = source_origin(meta)

    to_merc = Transformer.from_crs(WGS84, WEBMERCATOR, always_xy=True)
    from_merc = Transformer.from_crs(WEBMERCATOR, WGS84, always_xy=True)
    to_laea = Transformer.from_crs(WGS84, CRS.from_proj4(meta["projdef"]), always_xy=True)

    _, y_south = to_merc.transform(NESIS_WEST, NESIS_SOUTH)
    _, y_north = to_merc.transform(NESIS_EAST, NESIS_NORTH)
    x_west, _ = to_merc.transform(NESIS_WEST, NESIS_SOUTH)
    x_east, _ = to_merc.transform(NESIS_EAST, NESIS_NORTH)
    print(f"[nesis]  Target bbox in EPSG:3857 metres:")
    print(f"           x {x_west:.3f} … {x_east:.3f}")
    print(f"           y {y_south:.3f} … {y_north:.3f}")

    packed, ranks = build_rank_lut(meta["colormap"])

    out = np.zeros((height, width, 4), np.uint8)
    best = np.full((height, width), -(2 ** 30), np.int64)

    n = supersample
    for ky in range(n):
        for kx in range(n):
            # Sub-sample position inside each output pixel, in [0, 1).
            fx = (np.arange(width, dtype=np.float64) + (kx + 0.5) / n) / width
            fy = (np.arange(height, dtype=np.float64) + (ky + 0.5) / n) / height

            # u is linear in longitude; v is linear in Mercator y, north-down.
            lon = NESIS_WEST + fx * (NESIS_EAST - NESIS_WEST)
            merc_y = y_north - fy * (y_north - y_south)
            _, lat = from_merc.transform(np.zeros_like(merc_y), merc_y)

            grid_lon, grid_lat = np.meshgrid(lon, lat)
            gx, gy = to_laea.transform(grid_lon, grid_lat)

            col = np.floor((gx - x_ul) / px)
            row = np.floor((y_ul - gy) / px)
            inside = (
                np.isfinite(col) & np.isfinite(row)
                & (col >= 0) & (col < src_w) & (row >= 0) & (row < src_h)
            )
            ci = np.clip(col, 0, src_w - 1).astype(np.int32)
            ri = np.clip(row, 0, src_h - 1).astype(np.int32)

            sample = src[ri, ci]
            sample[~inside] = 0

            # Strongest sample wins: echo outranks non-echo, and among non-echo
            # the more opaque one (the coverage tint) beats full transparency.
            score = rank_of(sample[..., :3], packed, ranks).astype(np.int64) * 256
            score += sample[..., 3].astype(np.int64)
            score[~inside] = -(2 ** 29)

            take = score > best
            best = np.where(take, score, best)
            out[take] = sample[take]

    return out


# ---------------------------------------------------------------------------
# Alpha bleeding (colour dilation)
# ---------------------------------------------------------------------------

def _shift(a: np.ndarray, dy: int, dx: int) -> np.ndarray:
    """Shift *a* by (dy, dx) filling the vacated border with zeros."""
    out = np.zeros_like(a)
    h, w = a.shape[:2]
    ys_src = slice(max(0, -dy), h - max(0, dy))
    ys_dst = slice(max(0, dy), h - max(0, -dy))
    xs_src = slice(max(0, -dx), w - max(0, dx))
    xs_dst = slice(max(0, dx), w - max(0, -dx))
    out[ys_dst, xs_dst] = a[ys_src, xs_src]
    return out


def bleed_alpha(rgba: np.ndarray, rings: int = 2) -> np.ndarray:
    """
    Copy the RGB of opaque pixels outwards into their transparent neighbours,
    leaving alpha at zero.

    Nesis filters this texture with GL_LINEAR and blends it with straight
    alpha, so a transparent texel's RGB still contributes to the interpolated
    colour along every echo edge.  Without this the source's transparent pixels
    carry the bottom of the colour ramp and paint a cyan halo around each cell;
    the out-of-grid fill is transparent black and would paint a dark one.
    GL_LINEAR only ever samples a 2×2 texel neighbourhood, so a couple of rings
    is enough.
    """
    out = rgba.copy()
    filled = out[..., 3] > 0
    neighbours = ((-1, 0), (1, 0), (0, -1), (0, 1),
                  (-1, -1), (-1, 1), (1, -1), (1, 1))

    for _ in range(rings):
        rgb = out[..., :3]
        newly = np.zeros_like(filled)
        donor = np.zeros_like(rgb)
        for dy, dx in neighbours:
            take = _shift(filled, dy, dx) & ~filled & ~newly
            if not take.any():
                continue
            donor[take] = _shift(rgb, dy, dx)[take]
            newly |= take
        if not newly.any():
            break
        out[..., :3] = np.where(newly[..., None], donor, rgb)
        filled |= newly

    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()

    if args.width < 1:
        sys.exit(f"--width must be positive, got {args.width}.")
    if args.supersample < 1:
        sys.exit(f"--supersample must be at least 1, got {args.supersample}.")
    if args.height:
        height = args.height
    elif args.isotropic:
        height = round(args.width / NESIS_ASPECT)
    else:
        height = args.width
    if height < 1:
        sys.exit(f"--height must be positive, got {height}.")

    src, meta = read_source(args.input)
    print(f"[nesis]  Source {Path(args.input).name}: "
          f"{src.shape[1]}×{src.shape[0]} @ {meta['pixel_m']:.0f} m")
    print(f"[nesis]  Source projection: {meta['projdef']}")

    grid_size = f"{args.width}×{height}"
    print(f"[nesis]  Resampling to {grid_size} "
          f"with {args.supersample}×{args.supersample} supersampling …")
    out = reproject(src, meta, args.width, height, args.supersample)

    if not args.no_bleed:
        print("[nesis]  Bleeding colour into transparent edges for GL_LINEAR …")
        out = bleed_alpha(out)

    opaque = (out[..., 3] > 0).mean()
    print(f"[nesis]  {opaque * 100:.1f}% of output pixels carry data")

    try:
        from ecmwf_wind_map import save_png
    except ImportError as exc:
        sys.exit(
            f"Cannot import from ecmwf_wind_map.py: {exc}\n"
            "Run this script from the repository directory."
        )

    text = meta["text"]
    png_meta = {
        "Description":  "OPERA radar composite reprojected for the Nesis EU weather layer",
        "Projection":   "EPSG:3857 (spherical Web Mercator)",
        "BBox_deg":     f"W={NESIS_WEST} E={NESIS_EAST} S={NESIS_SOUTH} N={NESIS_NORTH}",
        "BBox_m":       "-1627293.369 5044402.235 3628618.637 11980434.095 (xmin xmax ymin ymax)",
        "PixelGrid":    "u linear in longitude, v linear in Mercator y; "
                        "bbox corners on pixel edges, centres at (i+0.5)/N",
        "Orientation":  "Row 0 is the north edge (Nesis calls QImage::mirrored)",
        "AlphaMode":    "Straight (non-premultiplied)",
        "AlphaBleed":   "none" if args.no_bleed else "2 rings of colour dilation",
        "GridSize":     grid_size,
        "Supersample":  str(args.supersample),
        "PixelAspect":  ("isotropic" if abs(args.width / height - NESIS_ASPECT) < 1e-4
                         else f"{(args.width / height) / NESIS_ASPECT:.4f}× wider than tall "
                              "in resolution terms"),
        "SourceImage":  Path(args.input).name,
        "SourceProjection": meta["projdef"],
        "NominalTime":  text.get("NominalTime", ""),
        "NominalTS":    text.get("NominalTS", ""),
        "Product":      text.get("Product", ""),
        "Colormap":     meta["colormap"],
        "SourceURL":    text.get("SourceURL", ""),
        "License":      text.get("License", ""),
    }
    save_png(out, args.output, png_meta)


if __name__ == "__main__":
    main()
