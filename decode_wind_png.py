#!/usr/bin/env python3
"""
decode_wind_png.py
------------------
Read a wind PNG produced by ecmwf_wind_map.py and recover U, V, speed arrays,
or visualise the data with matplotlib.

Usage
-----
  pip install pillow numpy matplotlib
  python decode_wind_png.py wind_uv.png [--plot]
"""

import argparse
import sys

import numpy as np


def decode(png_path: str, max_uv: float = 50.0, max_speed: float = 50.0):
    """
    Returns (U, V, speed) as float32 arrays in m/s.

    If the PNG was written by ecmwf_wind_map.py the encoding parameters are
    stored in the PNG text metadata and override the default arguments.
    """
    try:
        from PIL import Image
    except ImportError:
        sys.exit("Pillow not installed.  pip install pillow")

    img = Image.open(png_path)
    info = img.info  # dict from PNG text chunks

    # Read encoding parameters stored in the PNG (fall back to CLI defaults)
    try:
        max_uv    = float(info.get("MaxUV_ms",    max_uv))
        max_speed = float(info.get("MaxSpeed_ms", max_speed))
    except (KeyError, ValueError):
        pass

    print(f"PNG  : {img.width}×{img.height} px")
    print(f"MaxUV: {max_uv} m/s   MaxSpeed: {max_speed} m/s")
    if "Encoding" in info:
        print(f"Enc  : {info['Encoding']}")

    rgb = np.asarray(img).astype(np.float32)  # (H, W, 3) in [0, 255]
    R, G, B = rgb[..., 0], rgb[..., 1], rgb[..., 2]

    U     = (R / 255.0 * 2.0 - 1.0) * max_uv     # m/s
    V     = (G / 255.0 * 2.0 - 1.0) * max_uv     # m/s
    speed = B / 255.0 * max_speed                  # m/s

    print(f"U range    : {U.min():.2f} … {U.max():.2f} m/s")
    print(f"V range    : {V.min():.2f} … {V.max():.2f} m/s")
    print(f"Speed range: {speed.min():.2f} … {speed.max():.2f} m/s")

    return U, V, speed


def plot(png_path: str, U: np.ndarray, V: np.ndarray, speed: np.ndarray) -> None:
    try:
        import matplotlib.pyplot as plt
        import matplotlib.colors as mcolors
        from PIL import Image
    except ImportError:
        sys.exit("matplotlib / Pillow not installed.  pip install matplotlib pillow")

    H, W = speed.shape

    # Subsample for quiver arrows (otherwise too dense)
    step_y = max(1, H // 40)
    step_x = max(1, W // 80)
    ys = np.arange(0, H, step_y)
    xs = np.arange(0, W, step_x)
    Yq, Xq = np.meshgrid(ys, xs, indexing="ij")

    fig, axes = plt.subplots(1, 2, figsize=(18, 6))
    fig.suptitle("ECMWF 10-metre wind (decoded from UV-PNG)", fontsize=13)

    # --- Left: raw PNG ---
    axes[0].imshow(np.asarray(Image.open(png_path)))
    axes[0].set_title("Raw PNG  (R=U, G=V, B=speed)")
    axes[0].axis("off")

    # --- Right: wind speed + quiver ---
    lats = np.linspace(90, -90, H)
    lons = np.linspace(-180, 180, W)
    ax = axes[1]
    pcm = ax.pcolormesh(
        lons, lats, speed,
        cmap="plasma", shading="auto",
        norm=mcolors.Normalize(vmin=0, vmax=speed.max()),
    )
    fig.colorbar(pcm, ax=ax, label="wind speed (m/s)", fraction=0.03)

    ax.quiver(
        lons[Xq], lats[Yq],
        U[Yq, Xq], V[Yq, Xq],
        color="white", alpha=0.6, scale=700, width=0.0015,
    )
    ax.set_title("Wind speed + direction")
    ax.set_xlabel("Longitude (°)")
    ax.set_ylabel("Latitude (°)")

    plt.tight_layout()
    out = png_path.replace(".png", "_decoded.png")
    plt.savefig(out, dpi=120, bbox_inches="tight")
    print(f"Plot saved: {out}")
    plt.show()


def main():
    parser = argparse.ArgumentParser(description="Decode wind UV PNG")
    parser.add_argument("png", help="Input wind_uv.png")
    parser.add_argument("--plot", action="store_true", help="Render a matplotlib visualisation")
    parser.add_argument("--max-uv",    type=float, default=50.0)
    parser.add_argument("--max-speed", type=float, default=50.0)
    args = parser.parse_args()

    U, V, speed = decode(args.png, args.max_uv, args.max_speed)

    if args.plot:
        plot(args.png, U, V, speed)


if __name__ == "__main__":
    main()
