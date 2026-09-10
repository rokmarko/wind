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

---

## Installation

```bash
# Python packages
pip install ecmwf-opendata cfgrib xarray scipy pillow numpy

# For the OPERA rain radar module (opera_radar_map.py)
pip install h5py requests

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

## Data source (wind)

**ECMWF Open Data** — real-time HRES forecasts, updated 4× daily (00/06/12/18 UTC).
No API key required. Terms: https://apps.ecmwf.int/datasets/licences/general/
