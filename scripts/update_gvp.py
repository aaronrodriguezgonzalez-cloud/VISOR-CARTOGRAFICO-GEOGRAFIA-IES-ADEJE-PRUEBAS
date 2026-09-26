#!/usr/bin/env python3
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import json, time

BASE = "https://webservices.volcano.si.edu/geoserver/GVP-VOTW/ows"
OUT = Path("data")
OUT.mkdir(parents=True, exist_ok=True)

LAYERS = {
    "gvp_holocene_volcanoes.geojson": "GVP-VOTW:Smithsonian_VOTW_Holocene_Volcanoes",
    "gvp_holocene_eruptions.geojson": "GVP-VOTW:Smithsonian_VOTW_Holocene_Eruptions",
}

def download(layer):
    params = {
        "service": "WFS",
        "version": "1.0.0",
        "request": "GetFeature",
        "typeName": layer,
        "outputFormat": "application/json",
    }
    url = BASE + "?" + urlencode(params)
    req = Request(url, headers={"User-Agent": "ANAMBRO-educational-viewer/1.0"})
    last = None
    for attempt in range(4):
        try:
            with urlopen(req, timeout=180) as r:
                raw = r.read()
            data = json.loads(raw)
            if data.get("type") != "FeatureCollection" or not isinstance(data.get("features"), list):
                raise RuntimeError("La respuesta no es un FeatureCollection válido")
            if not data["features"]:
                raise RuntimeError("El servicio devolvió 0 entidades")
            return data
        except Exception as e:
            last = e
            if attempt < 3:
                time.sleep(5 * (attempt + 1))
    raise last

for filename, layer in LAYERS.items():
    data = download(layer)
    path = OUT / filename
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"{filename}: {len(data['features'])} entidades")
