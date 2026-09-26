#!/usr/bin/env python3
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
import json
import time

BASE = "https://webservices.volcano.si.edu/geoserver/GVP-VOTW/ows"
OUT = Path("data")
OUT.mkdir(parents=True, exist_ok=True)

LAYERS = {
    "gvp_holocene_volcanoes.geojson": "GVP-VOTW:Smithsonian_VOTW_Holocene_Volcanoes",
    "gvp_holocene_eruptions.geojson": "GVP-VOTW:Smithsonian_VOTW_Holocene_Eruptions",
}

PAGE_SIZE = 100
MAX_PAGES = 200
RETRIES = 5

def request_page(layer, start_index):
    params = {
        "service": "WFS",
        "version": "1.0.0",
        "request": "GetFeature",
        "typeName": layer,
        "outputFormat": "application/json",
        "maxFeatures": str(PAGE_SIZE),
        # GeoServer admite startIndex como parámetro de paginación.
        "startIndex": str(start_index),
    }
    url = BASE + "?" + urlencode(params)
    req = Request(
        url,
        headers={
            "User-Agent": "ANAMBRO-educational-viewer/1.0",
            "Accept": "application/json",
            "Connection": "close",
        },
    )

    last = None
    for attempt in range(RETRIES):
        try:
            with urlopen(req, timeout=90) as r:
                raw = r.read()
            data = json.loads(raw.decode("utf-8"))
            if data.get("type") != "FeatureCollection":
                raise RuntimeError("La respuesta del GVP no es un FeatureCollection válido")
            if not isinstance(data.get("features"), list):
                raise RuntimeError("La respuesta del GVP no contiene una lista de entidades")
            return data
        except (HTTPError, URLError, TimeoutError, ConnectionError, json.JSONDecodeError, RuntimeError) as e:
            last = e
            if attempt < RETRIES - 1:
                wait = 5 * (attempt + 1)
                print(
                    f"Reintento {attempt+1}/{RETRIES-1} para {layer}, "
                    f"startIndex={start_index}, en {wait}s: {e}",
                    flush=True,
                )
                time.sleep(wait)
    raise last

def download_paged(layer):
    features = []
    seen_ids = set()

    for page in range(MAX_PAGES):
        start = page * PAGE_SIZE
        data = request_page(layer, start)
        batch = data.get("features", [])

        if not batch:
            break

        added = 0
        for f in batch:
            fid = f.get("id")
            if fid is not None:
                if fid in seen_ids:
                    continue
                seen_ids.add(fid)
            features.append(f)
            added += 1

        print(
            f"{layer}: página {page+1}, recibidas {len(batch)}, "
            f"añadidas {added}, total {len(features)}",
            flush=True,
        )

        if len(batch) < PAGE_SIZE:
            break

        # Pequeña pausa para no castigar el servidor oficial.
        time.sleep(1.0)
    else:
        raise RuntimeError(
            f"Se alcanzó MAX_PAGES={MAX_PAGES}; abortando para evitar una descarga infinita"
        )

    if not features:
        raise RuntimeError(f"El servicio devolvió 0 entidades para {layer}")

    return {
        "type": "FeatureCollection",
        "features": features,
    }

for filename, layer in LAYERS.items():
    data = download_paged(layer)
    path = OUT / filename
    path.write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    print(f"{filename}: {len(data['features'])} entidades guardadas", flush=True)
