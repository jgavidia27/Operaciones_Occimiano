"""
build_comunas_geojson.py — genera comunas_chile.geojson para el planificador MP.

Por qué existe un script y no sólo el archivo: el asset es derivado, y sin la
receta nadie puede regenerarlo ni auditar de dónde salió cada polígono.

Dos fuentes, por calidad y por peso:

  • Región Metropolitana → límites oficiales del INE, publicados por el
    Observatorio de Ciudades UC en ArcGIS Hub. Es la etapa 1 del planificador y
    se mira a zoom de ciudad, así que se conserva casi intacta (~9 m de
    tolerancia). Santiago queda con 72 vértices.

  • Resto del país → caracena/chile-geojson, simplificado. A zoom regional el
    detalle fino no aporta, y Aysén y Magallanes se simplifican mucho más: sus
    fiordos aportaban el 70% de los vértices de todo Chile y ahí no hay EDS.

Una fuente descartada: fcortes/Chile-GeoJSON, que parecía servir por tamaño
pero trae la comuna de Santiago con 5 vértices —un cuadrilátero— y polígonos
que se meten en el mar. Los mapas salían con forma de octágono.

Uso:
    python build_comunas_geojson.py
"""

import json
import math
import os
import sys

import requests

sys.setrecursionlimit(50000)

SALIDA = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "comunas_chile.geojson")

RM_URL = ("https://services9.arcgis.com/kKJR3Qt68ohAWuet/arcgis/rest/services/"
          "Comuna_Regi%C3%B3n_Metropolitana/FeatureServer/0/query")
RM_PARAMS = {"where": "1=1", "outFields": "NOM_COMUNA,NOM_PROVIN,CUT",
             "outSR": "4326", "f": "geojson", "returnGeometry": "true",
             "resultRecordCount": 200}
REG_URL = "https://raw.githubusercontent.com/caracena/chile-geojson/master/{}.geojson"

EPS_RM = 0.00008          # ~9 m
EPS_REGIONES = 0.010      # ~1,1 km
EPS_AUSTRAL = 0.045       # ~5 km — Aysén (11) y Magallanes (12)


def perp(p, a, b):
    (x, y), (x1, y1), (x2, y2) = p, a, b
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return math.hypot(x - x1, y - y1)
    t = max(0, min(1, ((x - x1) * dx + (y - y1) * dy) / (dx * dx + dy * dy)))
    return math.hypot(x - (x1 + t * dx), y - (y1 + t * dy))


def dp(pts, eps):
    """Douglas-Peucker iterativo sobre una cadena ABIERTA."""
    if len(pts) < 3:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        dmax, idx = 0.0, i
        for k in range(i + 1, j):
            d = perp(pts[k], pts[i], pts[j])
            if d > dmax:
                dmax, idx = d, k
        if dmax > eps:
            keep[idx] = True
            stack.append((i, idx))
            stack.append((idx, j))
    return [p for p, k in zip(pts, keep) if k]


def simp_ring(ring, eps):
    """Un anillo CERRADO no se puede simplificar como cadena abierta: sus dos
    extremos son el mismo punto, la línea base queda degenerada y el polígono
    se deforma (de ahí salían las estrellas y octágonos). Se parte en el
    vértice más lejano al inicio y se simplifica cada mitad por separado."""
    pts = [tuple(p) for p in ring]
    if pts and pts[0] == pts[-1]:
        pts = pts[:-1]
    if len(pts) < 4:
        return None
    a = pts[0]
    far = max(range(len(pts)),
              key=lambda i: math.hypot(pts[i][0] - a[0], pts[i][1] - a[1]))
    s = dp(pts[:far + 1], eps)[:-1] + dp(pts[far:] + [pts[0]], eps)
    if len(s) < 4:
        return None
    if s[0] != s[-1]:
        s.append(s[0])
    return [[round(x, 5), round(y, 5)] for x, y in s]


def simplificar(geom, eps):
    gt, co = geom["type"], geom["coordinates"]
    if gt == "Polygon":
        nw = [x for x in (simp_ring(r, eps) for r in co) if x]
    else:
        nw = [p for p in ([x for x in (simp_ring(r, eps) for r in poly) if x]
                          for poly in co) if p]
    return {"type": gt, "coordinates": nw or co}


def contar(geom):
    co = geom["coordinates"]
    rings = co if geom["type"] == "Polygon" else [r for p in co for r in p]
    return sum(len(r) for r in rings)


def titulo(nombre: str) -> str:
    """El INE entrega los nombres en MAYÚSCULAS ('ÑUÑOA'); se normalizan a
    Title Case respetando las partículas ('Calera de Tango')."""
    menores = {"de", "del", "la", "las", "los", "y"}
    ps = str(nombre or "").strip().lower().split()
    return " ".join(p if i and p in menores else p.capitalize()
                    for i, p in enumerate(ps))


def main():
    feats, crudo = [], 0

    print("Región Metropolitana — INE / Observatorio de Ciudades UC…")
    r = requests.get(RM_URL, params=RM_PARAMS, timeout=180)
    r.raise_for_status()
    rm = r.json()
    if rm.get("exceededTransferLimit"):
        raise SystemExit("El servicio truncó la respuesta; hay que paginar.")
    for f in rm["features"]:
        crudo += contar(f["geometry"])
        feats.append({
            "type": "Feature",
            "properties": {"comuna": titulo(f["properties"]["NOM_COMUNA"]),
                           "region": "Metropolitana", "codregion": 13},
            "geometry": simplificar(f["geometry"], EPS_RM),
        })
    print(f"  {len(rm['features'])} comunas")

    print("Resto del país — caracena/chile-geojson…")
    for reg in range(1, 17):
        if reg == 13:
            continue
        g = requests.get(REG_URL.format(reg), timeout=180).json()
        eps = EPS_AUSTRAL if reg in (11, 12) else EPS_REGIONES
        for f in g["features"]:
            p = f.get("properties", {})
            crudo += contar(f["geometry"])
            feats.append({
                "type": "Feature",
                "properties": {"comuna": p.get("Comuna"), "region": p.get("Region"),
                               "codregion": p.get("codregion")},
                "geometry": simplificar(f["geometry"], eps),
            })
        print(f"  región {reg:2}: {len(g['features']):3} comunas")

    s = json.dumps({"type": "FeatureCollection", "features": feats},
                   ensure_ascii=False, separators=(",", ":"))
    open(SALIDA, "w", encoding="utf-8").write(s)
    fin = sum(contar(f["geometry"]) for f in feats)
    rm_pts = sum(contar(f["geometry"]) for f in feats
                 if f["properties"]["codregion"] == 13)
    print(f"\n{SALIDA}")
    print(f"  comunas: {len(feats)} | vértices: {crudo:,} → {fin:,} "
          f"({fin / crudo * 100:.0f}%)")
    print(f"  RM: {rm_pts:,} vértices | archivo: {len(s) / 1024:.0f} KB")


if __name__ == "__main__":
    main()
