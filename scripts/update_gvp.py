#!/usr/bin/env python3
from pathlib import Path
from urllib.parse import urljoin
from urllib.request import Request, urlopen
from html import unescape
import json, re, time, zipfile
import xml.etree.ElementTree as ET
from io import BytesIO

OUT = Path("data")
OUT.mkdir(parents=True, exist_ok=True)

VOLCANO_DOWNLOAD = "https://volcano.si.edu/database/list_volcano_holocene_excel.cfm"
ERUPTION_PAGE = "https://volcano.si.edu/search_eruption.cfm"
UA = "ANAMBRO-educational-viewer/1.0 (+IES Adeje; source: Smithsonian GVP)"
RETRIES = 5

def fetch_bytes(url, timeout=120):
    last = None
    for attempt in range(RETRIES):
        try:
            req = Request(url, headers={
                "User-Agent": UA,
                "Accept": "*/*",
                "Connection": "close",
                "Referer": "https://volcano.si.edu/",
            })
            with urlopen(req, timeout=timeout) as r:
                data = r.read()
            if not data:
                raise RuntimeError("respuesta vacía")
            return data
        except Exception as e:
            last = e
            if attempt < RETRIES - 1:
                wait = 5 * (attempt + 1)
                print(f"Reintento {attempt+1}/{RETRIES-1} en {wait}s: {e}", flush=True)
                time.sleep(wait)
    raise last

def find_current_eruption_xlsx():
    html = fetch_bytes(ERUPTION_PAGE, 60).decode("utf-8", "replace")
    hrefs = re.findall(
        r'href\s*=\s*["\']([^"\']+GVP_Eruption_List_Holocene_[^"\']+\.xlsx)["\']',
        html, flags=re.I
    )
    if not hrefs:
        raise RuntimeError("No se encontró el Excel oficial de erupciones holocenas.")
    return urljoin(ERUPTION_PAGE, unescape(hrefs[0]))

def norm(s):
    return re.sub(r"[^a-z0-9]+", "", str(s or "").strip().lower())

def clean(v):
    if v is None:
        return None
    if isinstance(v, str):
        v = v.strip()
        return v if v else None
    return v

def parse_number(v):
    if v is None:
        return None
    s = str(v).strip().replace(",", ".")
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if not m:
        return None
    n = float(m.group(0))
    up = s.upper()
    if ("S" in up or "W" in up or "O" in up) and n > 0:
        n = -n
    return n

def clean_spreadsheet_xml(raw):
    # El XML Excel que genera actualmente el Smithsonian puede contener
    # caracteres de control o ampersands sin escapar. Excel los tolera,
    # pero ElementTree no. Normalizamos SOLO la sintaxis XML; no alteramos datos.
    enc = "utf-8"
    head = raw[:300].decode("ascii", "ignore")
    m = re.search(r'encoding=["\\\']([^"\\\']+)', head, flags=re.I)
    if m:
        enc = m.group(1)
    try:
        s = raw.decode(enc, "replace")
    except LookupError:
        s = raw.decode("utf-8", "replace")

    # Eliminar caracteres prohibidos por XML 1.0, conservando tab/CR/LF.
    s = "".join(
        ch for ch in s
        if ch in "\\t\\n\\r" or ord(ch) >= 0x20
    )

    # Escapar solo ampersands "sueltos"; conservar entidades XML válidas.
    s = re.sub(
        r'&(?!amp;|lt;|gt;|quot;|apos;|#\\d+;|#x[0-9A-Fa-f]+;)',
        '&amp;',
        s
    )

    # El XML que está devolviendo actualmente el Smithsonian llega con
    # atributos pegados al nombre de la etiqueta, por ejemplo:
    # <Workbookxmlns="..."xmlns:ss="...">
    # Insertamos únicamente los espacios sintácticos que faltan.
    s = re.sub(
        r'(<[A-Za-z_][A-Za-z0-9_.:-]*)(xmlns(?::[A-Za-z_][A-Za-z0-9_.-]*)?=)',
        r'\1 \2',
        s
    )
    s = re.sub(
        r'''(["'])(xmlns(?::[A-Za-z_][A-Za-z0-9_.-]*)?=)''',
        r'\1 \2',
        s
    )
    s = re.sub(
        r'''(["'])(ss:[A-Za-z_][A-Za-z0-9_.-]*=)''',
        r'\1 \2',
        s
    )
    return s

def rows_from_xml_spreadsheet(raw):
    cleaned = clean_spreadsheet_xml(raw)
    try:
        root = ET.fromstring(cleaned)
    except ET.ParseError as e:
        line, col = getattr(e, "position", (None, None))
        snippet = ""
        if line is not None:
            lines = cleaned.splitlines()
            if 1 <= line <= len(lines):
                a = max(0, col - 160)
                b = min(len(lines[line-1]), col + 160)
                snippet = lines[line-1][a:b]
        raise RuntimeError(
            f"No se pudo interpretar el XML Excel oficial del Smithsonian "
            f"(línea {line}, columna {col}). Fragmento: {snippet!r}"
        ) from e
    ns = {"ss": "urn:schemas-microsoft-com:office:spreadsheet"}
    rows = []
    for row in root.findall(".//ss:Row", ns):
        vals = []
        for cell in row.findall("ss:Cell", ns):
            idx = cell.attrib.get("{urn:schemas-microsoft-com:office:spreadsheet}Index")
            if idx:
                while len(vals) < int(idx) - 1:
                    vals.append(None)
            data = cell.find("ss:Data", ns)
            vals.append(data.text if data is not None else None)
        rows.append(vals)
    if not rows:
        raise RuntimeError("El Excel XML de volcanes no contiene filas.")
    headers = [str(x or "").strip() for x in rows[0]]
    out = []
    for row in rows[1:]:
        row = row + [None] * max(0, len(headers) - len(row))
        d = {headers[i]: clean(row[i]) for i in range(len(headers)) if headers[i]}
        if any(v is not None for v in d.values()):
            out.append(d)
    return out

def xlsx_shared_strings(z):
    try:
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    return ["".join(t.text or "" for t in si.findall(".//m:t", ns))
            for si in root.findall("m:si", ns)]

def xlsx_first_sheet_path(z):
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    ns = {
        "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
        "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    }
    sheet = wb.find(".//m:sheets/m:sheet", ns)
    if sheet is None:
        raise RuntimeError("XLSX sin hojas.")
    rid = sheet.attrib["{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"]
    rel = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    rns = {"p": "http://schemas.openxmlformats.org/package/2006/relationships"}
    for r in rel.findall("p:Relationship", rns):
        if r.attrib.get("Id") == rid:
            target = r.attrib["Target"].lstrip("/")
            return target if target.startswith("xl/") else "xl/" + target
    raise RuntimeError("No se pudo resolver la hoja XLSX.")

def rows_from_xlsx(raw):
    z = zipfile.ZipFile(BytesIO(raw))
    shared = xlsx_shared_strings(z)
    root = ET.fromstring(z.read(xlsx_first_sheet_path(z)))
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    rows = []
    for row in root.findall(".//m:sheetData/m:row", ns):
        values = {}
        for c in row.findall("m:c", ns):
            ref = c.attrib.get("r", "")
            col_letters = re.match(r"[A-Z]+", ref)
            if not col_letters:
                continue
            col = 0
            for ch in col_letters.group(0):
                col = col * 26 + (ord(ch) - 64)
            col -= 1
            typ = c.attrib.get("t")
            v = c.find("m:v", ns)
            inline = c.find("m:is", ns)
            val = None
            if typ == "s" and v is not None:
                i = int(v.text)
                val = shared[i] if i < len(shared) else None
            elif typ == "inlineStr" and inline is not None:
                val = "".join(t.text or "" for t in inline.findall(".//m:t", ns))
            elif v is not None:
                val = v.text
            values[col] = clean(val)
        if values:
            width = max(values) + 1
            rows.append([values.get(i) for i in range(width)])
    if not rows:
        raise RuntimeError("El XLSX de erupciones no contiene filas.")
    headers = [str(x or "").strip() for x in rows[0]]
    out = []
    for row in rows[1:]:
        row = row + [None] * max(0, len(headers) - len(row))
        d = {headers[i]: clean(row[i]) for i in range(len(headers)) if headers[i]}
        if any(v is not None for v in d.values()):
            out.append(d)
    return out

def value(row, *aliases):
    nmap = {norm(k): v for k, v in row.items()}
    for a in aliases:
        n = norm(a)
        if n in nmap and nmap[n] is not None:
            return nmap[n]
    return None

def add_aliases_volcano(row):
    p = dict(row)
    aliases = {
        "Volcano_Number": value(row, "Volcano Number", "VolcanoNumber", "Volcano_Number"),
        "Volcano_Name": value(row, "Volcano Name", "VolcanoName", "Volcano_Name", "Name"),
        "Country": value(row, "Country"),
        "Volcanic_Region": value(row, "Volcanic Region", "VolcanicRegion", "Region"),
        "Volcano_Landform": value(row, "Volcano Landform", "VolcanoLandform", "Primary Volcano Type"),
        "Primary_Volcano_Type": value(row, "Primary Volcano Type", "PrimaryVolcanoType", "Volcano Type"),
        "Elevation": value(row, "Elevation", "Elevation (m)", "Elevation_m"),
        "Latitude": value(row, "Latitude", "Decimal Latitude", "LocationLatitude"),
        "Longitude": value(row, "Longitude", "Decimal Longitude", "LocationLongitude"),
        "Last_Eruption_Year": value(row, "Last Eruption Year", "LastEruptionYear"),
        "Tectonic_Setting": value(row, "Tectonic Setting", "TectonicSetting"),
        "Geologic_Evidence": value(row, "Geologic Evidence", "GeologicEvidence"),
        "Major_Rock_1": value(row, "Major Rock 1", "MajorRock1"),
        "Major_Rock_2": value(row, "Major Rock 2", "MajorRock2"),
        "Major_Rock_3": value(row, "Major Rock 3", "MajorRock3"),
        "Major_Rock_4": value(row, "Major Rock 4", "MajorRock4"),
        "Major_Rock_5": value(row, "Major Rock 5", "MajorRock5"),
    }
    p.update({k: v for k, v in aliases.items() if v is not None})
    return p

def add_aliases_eruption(row):
    p = dict(row)
    aliases = {
        "Volcano_Number": value(row, "Volcano Number", "VolcanoNumber", "Volcano_Number"),
        "Volcano_Name": value(row, "Volcano Name", "VolcanoName", "Volcano_Name"),
        "Eruption_Number": value(row, "Eruption Number", "EruptionNumber", "Eruption_Number"),
        "Activity_Type": value(row, "Activity Type", "ActivityType"),
        "ActivityArea": value(row, "Activity Area", "ActivityArea"),
        "ActivityUnit": value(row, "Activity Unit", "ActivityUnit"),
        "ExplosivityIndexMax": value(row, "VEI", "Explosivity Index Max", "ExplosivityIndexMax"),
        "ExplosivityIndexModifier": value(row, "VEI Modifier", "ExplosivityIndexModifier"),
        "StartEvidenceMethod": value(row, "Start Evidence Method", "StartEvidenceMethod", "Start Evidence"),
        "StartDateYearModifier": value(row, "Start Date Year Modifier", "StartDateYearModifier"),
        "StartDateYear": value(row, "Start Date Year", "StartDateYear", "Start Year"),
        "StartDateYearUncertainty": value(row, "Start Date Year Uncertainty", "StartDateYearUncertainty"),
        "StartDateMonth": value(row, "Start Date Month", "StartDateMonth", "Start Month"),
        "StartDateDay": value(row, "Start Date Day", "StartDateDay", "Start Day"),
        "EndDateYearModifier": value(row, "End Date Year Modifier", "EndDateYearModifier"),
        "EndDateYear": value(row, "End Date Year", "EndDateYear", "End Year"),
        "EndDateYearUncertainty": value(row, "End Date Year Uncertainty", "EndDateYearUncertainty"),
        "EndDateMonth": value(row, "End Date Month", "EndDateMonth", "End Month"),
        "EndDateDay": value(row, "End Date Day", "EndDateDay", "End Day"),
    }
    p.update({k: v for k, v in aliases.items() if v is not None})
    return p

def volcano_geojson(rows):
    features = []
    for row in rows:
        p = add_aliases_volcano(row)
        lat = parse_number(p.get("Latitude"))
        lon = parse_number(p.get("Longitude"))
        geom = None
        if lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180:
            geom = {"type": "Point", "coordinates": [lon, lat]}
        features.append({"type": "Feature", "properties": p, "geometry": geom})
    valid_points = sum(1 for f in features if f["geometry"])
    if valid_points < 500:
        raise RuntimeError(f"Solo {valid_points} volcanes tienen coordenadas válidas; se aborta.")
    print(f"Volcanes: {len(features)} registros; {valid_points} con coordenadas.", flush=True)
    return {"type": "FeatureCollection", "features": features}

def eruption_geojson(rows):
    features = [{"type": "Feature", "properties": add_aliases_eruption(r), "geometry": None}
                for r in rows]
    if len(features) < 5000:
        raise RuntimeError(f"Solo se obtuvieron {len(features)} erupciones; se aborta.")
    print(f"Erupciones: {len(features)} registros.", flush=True)
    return {"type": "FeatureCollection", "features": features}

print("Descargando listado oficial de volcanes holocenos…", flush=True)
volcano_raw = fetch_bytes(VOLCANO_DOWNLOAD, 120)
probe = volcano_raw[:500].lstrip().lower()
if probe.startswith(b"<!doctype html") or probe.startswith(b"<html"):
    raise RuntimeError(
        "El Smithsonian devolvió HTML en lugar del archivo Excel de volcanes. "
        "No se guardará ningún dato incompleto."
    )
volcano_rows = rows_from_xlsx(volcano_raw) if volcano_raw[:2] == b"PK" else rows_from_xml_spreadsheet(volcano_raw)
volcano_data = volcano_geojson(volcano_rows)

eruption_url = find_current_eruption_xlsx()
print(f"Descargando Excel oficial de erupciones: {eruption_url}", flush=True)
eruption_raw = fetch_bytes(eruption_url, 180)
eruption_data = eruption_geojson(rows_from_xlsx(eruption_raw))

(OUT / "gvp_holocene_volcanoes.geojson").write_text(
    json.dumps(volcano_data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
)
(OUT / "gvp_holocene_eruptions.geojson").write_text(
    json.dumps(eruption_data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
)
print("Datos GVP oficiales guardados correctamente.", flush=True)
