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

---

## Installation

```bash
# Python packages
pip install ecmwf-opendata cfgrib xarray scipy pillow numpy

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

## Data source

**ECMWF Open Data** — real-time HRES forecasts, updated 4× daily (00/06/12/18 UTC).
No API key required. Terms: https://apps.ecmwf.int/datasets/licences/general/
