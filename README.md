# ECMWF Wind UV Map

Fetches the latest **ECMWF HRES open-data** global wind forecast and
generates a **2048 × 1024 PNG** with U and V wind components encoded
directly in the RGB channels — ready to use as a texture in WebGL, deck.gl,
Mapbox wind layers, or any other renderer.

---

## Files

| File | Purpose |
|------|---------|
| `ecmwf_wind_map.py` | Download + encode → PNG |
| `decode_wind_png.py` | Decode PNG back to m/s arrays + optional plot |
| `opera_radar_map.py` | Fetch EUMETNET OPERA rain radar composite → colourised PNG |
| `nesis_radar_png.py` | Reproject that PNG to the Nesis EU weather texture (EPSG:3857) |

---

## Installation

```bash
# Python packages
pip install ecmwf-opendata cfgrib xarray scipy pillow numpy

# For the OPERA rain radar module (opera_radar_map.py)
pip install h5py requests

# For the Nesis converter (nesis_radar_png.py)
pip install pyproj

# eccodes C library (required by cfgrib)
# macOS
brew install eccodes

# Ubuntu / Debian
sudo apt-get install libeccodes-dev

# conda (any platform)
conda install -c conda-forge eccodes
```

---

## Quick start

```bash
# Fetch analysis (T+0) and write wind_uv.png
python ecmwf_wind_map.py

# Fetch T+24h forecast
python ecmwf_wind_map.py --step 24 --output wind_24h.png

# Custom resolution and wind range
python ecmwf_wind_map.py --width 4096 --height 2048 --max-uv 60 --max-speed 60
```

---

## PNG encoding

| Channel | Encodes | Formula (p ∈ [0, 255]) |
|---------|---------|------------------------|
| **R** | U-component (W→E, m/s) | `U = (R/255 × 2 − 1) × MAX_UV` |
| **G** | V-component (S→N, m/s) | `V = (G/255 × 2 − 1) × MAX_UV` |
| **B** | Wind speed magnitude (m/s) | `speed = B/255 × MAX_SPEED` |

- **128** in R or G → 0 m/s (neutral).
- **0** in R → maximum westward wind (−MAX_UV m/s).
- **255** in R → maximum eastward wind (+MAX_UV m/s).
- Default `MAX_UV = MAX_SPEED = 50 m/s` (covers all but the most extreme jet-stream events).
- Encoding parameters are embedded as PNG text metadata so the decoder can read them automatically.

### Projection

Equirectangular, whole globe:

```
top-left  pixel → (90 °N, 180 °W)
bottom-right pixel → (90 °S, 180 °E)
```

---

## Decoding

```bash
# Print stats
python decode_wind_png.py wind_uv.png

# Stats + matplotlib visualisation saved as wind_uv_decoded.png
python decode_wind_png.py wind_uv.png --plot
```

In Python / NumPy:

```python
from PIL import Image
import numpy as np

MAX_UV    = 50.0   # m/s  (read from PNG metadata or match your encode settings)
MAX_SPEED = 50.0

img = np.asarray(Image.open("wind_uv.png")).astype(np.float32)
R, G, B = img[..., 0], img[..., 1], img[..., 2]

U     = (R / 255.0 * 2.0 - 1.0) * MAX_UV     # m/s, west–east
V     = (G / 255.0 * 2.0 - 1.0) * MAX_UV     # m/s, south–north
speed = B / 255.0 * MAX_SPEED                  # m/s
```

### WebGL (GLSL) snippet

```glsl
uniform sampler2D windTexture;
uniform float maxUV;      // e.g. 50.0
uniform float maxSpeed;   // e.g. 50.0

vec4 px = texture2D(windTexture, uv);
float u     = (px.r * 2.0 - 1.0) * maxUV;
float v     = (px.g * 2.0 - 1.0) * maxUV;
float speed =  px.b * maxSpeed;
```

---

## CLI reference

```
usage: ecmwf_wind_map.py [-h] [--step STEP] [--output OUTPUT]
                          [--max-speed MAX_SPEED] [--max-uv MAX_UV]
                          [--width WIDTH] [--height HEIGHT]

  --step        Forecast hour offset, e.g. 0, 6, 12, 24 …  (default: 0)
  --output      Output PNG path                             (default: wind_uv.png)
  --max-uv      ±clamp for U/V before encoding              (default: 50 m/s)
  --max-speed   Speed that maps to B=255                    (default: 50 m/s)
  --width       Output width  in pixels                     (default: 2048)
  --height      Output height in pixels                     (default: 1024)
```

---

## Rain radar over Europe (OPERA)

`opera_radar_map.py` is an independent second data source: the **EUMETNET OPERA**
European weather-radar composite. Unlike the wind PNGs, its output is *not* an
encoding — it is a directly viewable **colourised RGBA image**, transparent
wherever it is not raining or the radar network has no coverage.

```bash
# Newest frame, native 1 km grid → opera_dbzh.png (3800 × 4400)
python opera_radar_map.py

# Half resolution, with the radar network footprint faintly visible
python opera_radar_map.py --scale 2 --show-coverage --output radar_2km.png

# One specific frame from the 24-hour cache
python opera_radar_map.py --time 20260910T0900

# What is currently available?
python opera_radar_map.py --list
```

### Data source

The **CIRRUS maximum reflectivity** composite (`DBZH`, dBZ), updated every
**5 minutes** and typically published ~4 minutes after nominal time. It is read
straight from the anonymous Open Radar Data S3 cache — **no API key, no rate
limit**, so the MeteoGate REST gateway is not used:

```
https://s3.waw3-1.cloudferro.com/openradar-24h/
    {YYYY}/{MM}/{DD}/OPERA/COMP/OPERA@{YYYYMMDD}T{HHMM}@0@DBZH.h5
```

Only the last 24 hours are cached. Files are ODIM_H5 (`ODIM_H5/V2_4`), read with
`h5py`. Documentation: https://eumetnet.github.io/openradardata-documentation/

> **Licence: CC BY 4.0.** Attribution is required when publishing the image:
> *© EUMETNET OPERA — Open Radar Data (RODEO), CC BY 4.0*

### Projection

The image is written on the composite's **native grid, 1:1** — no resampling, so
it does *not* line up with the equirectangular wind PNGs without a
projection-aware renderer. The grid is Lambert Azimuthal Equal Area:

```
+proj=laea +lat_0=55.0 +lon_0=10.0 +x_0=1950000.0 +y_0=-2100000.0 +units=m +ellps=WGS84

3800 × 4400 px @ 1000 m        row 0 = northern edge, column 0 = western edge
corners:  UL 67.02 °N  39.54 °W      UR 67.62 °N  57.81 °E
          LL 31.75 °N  10.43 °W      LR 31.99 °N  29.42 °E
```

The projection string and all four corner lat/lons are embedded as PNG text
metadata, so a consumer can georeference the image without touching the source
file.

### Colour scale and transparency

Classic NWS reflectivity ramp, linearly interpolated between stops:

| dBZ | 5 | 15 | 20 | 30 | 35 | 45 | 50 | 65 | 75 |
|-----|---|----|----|----|----|----|----|----|-----|
| | cyan | blue | green | dark green | yellow | orange | red | magenta | white |

The source distinguishes three states, and the alpha channel preserves that
distinction:

| Source state | Share of grid | Alpha |
|--------------|---------------|-------|
| Echo ≥ `--min-dbz` | ~6 % | 255 (ramping in over the first 5 dB) |
| Detected but below threshold | — | 0 |
| `undetect` — in coverage, dry | ~41 % | 0, or a faint tint with `--show-coverage` |
| `nodata` — outside radar coverage | ~49 % | 0 |

`--min-dbz` defaults to **5.0**, which suppresses clutter and clear-air noise.
`--scale N` reduces the grid by an integer factor using a block **maximum**, not
a mean, so storm cores are not averaged away.

### CLI reference

```
usage: opera_radar_map.py [-h] [--output OUTPUT] [--time TIME] [--scale SCALE]
                          [--min-dbz MIN_DBZ] [--show-coverage] [--list]

  --output          Output PNG path                     (default: opera_dbzh.png)
  --time            Specific frame, YYYYMMDDTHHMM       (default: newest available)
  --scale           Integer block-max reduction factor  (default: 1 = native 1 km)
  --min-dbz         Transparent below this reflectivity (default: 5.0 dBZ)
  --show-coverage   Tint in-coverage-but-dry pixels so the footprint is visible
  --list            List available timestamps and exit
```

---

## Nesis EU weather texture

`nesis_radar_png.py` reprojects the OPERA PNG into the fixed texture the Nesis
`WeatherImageRenderer` expects. It reads the source's projection straight out of
the PNG text chunks, so the two scripts chain with no arguments in between:

```bash
python opera_radar_map.py --output opera_dbzh.png
python nesis_radar_png.py opera_dbzh.png --output nesis_weather.png
```

### What the target format is, and why

Every requirement below comes from the Nesis sources rather than convention:

| Property | Requirement | Where it comes from |
|----------|-------------|---------------------|
| Projection | **Spherical** Mercator, EPSG:3857 | `OGLSphereTriangulator.cpp` `Triangulate2()` computes `fY = log(tan(M_PI_4 + fLat/2))` — no eccentricity term |
| Extent | W −14.618225054687514° E 45.314636273437486°<br>S 30.968189526345665° N 72.62025190354672° | `WeatherImageRenderer.cpp:57-58` |
| Orientation | Row 0 = **north** edge | `Triangulate2` puts `v=0` at the south; `Update()` calls `QImage::mirrored()` |
| Pixel grid | `u` linear in longitude, `v` linear in Mercator y, bbox corners on pixel **edges** | GL texture coords 0/1 sit on the texture's outer edges |
| Format | 8-bit RGBA, **straight** (non-premultiplied) alpha | `Format_RGBA8888`; `Weather.fsh` does `vec4(color.rgb, color.a*glf_alpha)` |
| Size | Free — `u`/`v` are normalised | Natural square-metre aspect is `w/h = 0.798832` |

In EPSG:3857 metres the extent is
`-1627293.369 … 5044402.235` x by `3628618.637 … 11980434.095` y.

> **EPSG:3395 would be wrong.** Ellipsoidal Mercator puts the north edge 40 km
> off at 72.6°N. So would EPSG:4326 — in plate carrée the latitude grid lines are
> evenly spaced, whereas here they must widen toward the north.

### Resampling

The source is a 1 km grid and the target is roughly 3–4× coarser on the ground,
so each output pixel takes the **strongest** of N×N sub-samples
(`--supersample`, default 3) rather than a single nearest neighbour — the usual
way radar products are reduced. At 1024 px wide this retains about 23 % more
echo than plain nearest-neighbour.

Sub-samples are ranked by position along the source's own colour ramp, which the
converter reads from the `Colormap` text chunk. Only whole source pixels are ever
copied, never blended, so the output contains exactly the ramp colours of the
input — no invented intermediate hues, and no premultiplication.

### Alpha bleeding

Nesis filters the texture with `GL_LINEAR` and blends with straight alpha, so
the RGB of a *transparent* texel still contributes along every echo edge. Left
alone, the source's transparent pixels carry the bottom of the colour ramp and
paint a cyan halo around each cell, and the out-of-grid fill is transparent black
and would paint a dark one. The converter therefore dilates opaque colours two
rings outwards into transparent pixels, leaving alpha at zero. `--no-bleed`
disables it.

### CLI reference

```
usage: nesis_radar_png.py [-h] [--output OUTPUT] [--width WIDTH]
                          [--height HEIGHT] [--supersample SUPERSAMPLE]
                          [--no-bleed] input

  input           Input PNG from opera_radar_map.py
  --output        Output PNG path                    (default: nesis_weather.png)
  --width         Output width in pixels             (default: 1024)
  --height        Output height in pixels            (default: width / 0.798832)
  --supersample   Strongest of N×N sub-samples       (default: 3; 1 = nearest)
  --no-bleed      Skip colour dilation into transparent pixels
```

---

## Data source (wind)

**ECMWF Open Data** — real-time HRES forecasts, updated 4× daily (00/06/12/18 UTC).
No API key required. Terms: https://apps.ecmwf.int/datasets/licences/general/
