"""
sync_eds_geo.py
===============
Puebla `eds_geo`: latitud, longitud y comuna de cada EDS.

Por qué existe: el planificador de MP (mp_planner.py) arma las rutas por
cercanía geográfica, y para eso necesita coordenadas. Fracttal las tiene en el
maestro de ítems (cada ubicación LOC-xxx), pero `/api/locations` NO existe en
la API — hay que recorrer `/api/items` y quedarse con los que son ubicaciones.

Ojo con los nombres de campo, que no son simétricos:
    latitude  -> latitud            longitud  -> longitud  (NO "longitude")
    field_1   -> cliente            field_2   -> dirección
    field_3   -> comuna             field_5   -> región

El campo `comuna` de `estaciones_servicio` viene sucio ("O", "Aeropuerto",
"MALLOCO"); `field_3` de Fracttal está completo al 100% y es la fuente buena.
Sólo hay que normalizar la ortografía ("Peñalolen" -> "Peñalolén") y las
variantes de región ("Metropolitana" / "Región Metropolitana").

Se mantiene en tabla aparte para que el sync de `estaciones_servicio` no la
pise.

Uso:
    python sync_eds_geo.py
    python sync_eds_geo.py --dry-run
"""

import argparse
import re
import time
import unicodedata

import requests

from sync_numerales_subtarea import (
    SUPABASE_URL, SUPABASE_KEY, FRACTTAL_BASE, ID_COMPANY,
    get_token, _sb_headers, log,
)

TABLE = "eds_geo"
PAGE = 100          # la API tope ~100 registros por página, ignora limit mayores

# Comunas de la RM + principales comunas/ciudades del resto del país. El match
# es por texto normalizado, así que sirve tanto "Ñuñoa" como "ÑUÑOA".
COMUNAS_RM = [
    "Cerrillos", "Cerro Navia", "Conchalí", "El Bosque", "Estación Central",
    "Huechuraba", "Independencia", "La Cisterna", "La Florida", "La Granja",
    "La Pintana", "La Reina", "Las Condes", "Lo Barnechea", "Lo Espejo",
    "Lo Prado", "Macul", "Maipú", "Ñuñoa", "Pedro Aguirre Cerda", "Peñalolén",
    "Providencia", "Pudahuel", "Quilicura", "Quinta Normal", "Recoleta",
    "Renca", "San Joaquín", "San Miguel", "San Ramón", "Santiago", "Vitacura",
    "Puente Alto", "San Bernardo", "Padre Hurtado", "Colina", "Lampa", "Buin",
    "Paine", "Melipilla", "Talagante", "Peñaflor", "Calera de Tango",
    "San José de Maipo", "Pirque", "Isla de Maipo", "El Monte", "Curacaví",
    "Til Til", "Malloco",
]
COMUNAS_REG = [
    "Arica", "Iquique", "Alto Hospicio", "Antofagasta", "Calama", "Tocopilla",
    "Mejillones", "Copiapó", "Vallenar", "Caldera", "La Serena", "Coquimbo",
    "Ovalle", "Illapel", "Los Vilos", "Valparaíso", "Viña del Mar", "Quilpué",
    "Villa Alemana", "San Antonio", "Quillota", "La Calera", "San Felipe",
    "Los Andes", "Casablanca", "Concón", "Limache", "Rancagua", "Machalí",
    "San Fernando", "Rengo", "Santa Cruz", "Talca", "Curicó", "Linares",
    "Constitución", "Chillán", "San Carlos", "Concepción", "Talcahuano",
    "San Pedro de la Paz", "Hualpén", "Chiguayante", "Coronel", "Lota",
    "Los Ángeles", "Temuco", "Padre Las Casas", "Villarrica", "Angol",
    "Valdivia", "La Unión", "Osorno", "Puerto Montt", "Puerto Varas",
    "Castro", "Ancud", "Coyhaique", "Punta Arenas", "Puerto Natales",
]


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s or "").upper())
    s = s.encode("ascii", "ignore").decode()
    return " ".join(re.sub(r"[^A-Z0-9 ]", " ", s).split())


# Se ordena de más largo a más corto para que "La Serena" gane sobre "Serena"
# y "San Pedro de la Paz" sobre "San Pedro".
_TABLA = sorted(((_norm(c), c) for c in COMUNAS_RM + COMUNAS_REG),
                key=lambda t: -len(t[0]))


_CANON = {_norm(c): c for c in COMUNAS_RM + COMUNAS_REG}


def canonizar(valor: str):
    """Ortografía canónica de una comuna ('PEÑALOLEN', 'Peñalolen' -> 'Peñalolén').
    Si no está en el listado conocido, devuelve el valor tal cual capitalizado:
    perder el dato sería peor que arrastrar una grafía distinta."""
    v = str(valor or "").strip()
    if not v:
        return None
    return _CANON.get(_norm(v), v.title())


def comuna_de(descripcion: str):
    """Comuna contenida en el nombre de la ubicación, o None.
    Fallback para las pocas ubicaciones sin field_3."""
    d = _norm(re.sub(r"\{\s*LOC-\d+\s*\}", "", str(descripcion or "")))
    for nrm, com in _TABLA:
        if re.search(rf"(^| ){re.escape(nrm)}( |$)", d):
            return com
    return None


def region_de(valor: str):
    """Normaliza las variantes de región ('Región Metropolitana' -> 'Metropolitana')."""
    v = _norm(valor)
    if not v:
        return None
    v = re.sub(r"^REGION (DE |DEL |DE LA )?", "", v)
    equiv = {"METROPOLITANA": "Metropolitana", "VALPARAISO": "Valparaíso",
             "COQUIMBO": "Coquimbo", "ANTOFAGASTA": "Antofagasta",
             "ATACAMA": "Atacama", "BIO BIO": "Biobío", "BIOBIO": "Biobío",
             "OHIGGINS": "O'Higgins", "O HIGGINS": "O'Higgins",
             "ARICA Y PARINACOTA": "Arica y Parinacota", "TARAPACA": "Tarapacá",
             "MAULE": "Maule", "NUBLE": "Ñuble", "ARAUCANIA": "Araucanía",
             "LOS RIOS": "Los Ríos", "LOS LAGOS": "Los Lagos",
             "AYSEN": "Aysén", "MAGALLANES": "Magallanes"}
    return equiv.get(v, str(valor).strip().title())


def ubicaciones_fracttal(token: str) -> list:
    """Ubicaciones (LOC-xxx) con coordenadas, desde el maestro de ítems."""
    h = {"Authorization": f"Bearer {token}", "ID-COMPANY": str(ID_COMPANY)}
    out, start = [], 0
    while True:
        # El maestro de ítems son ~2.000 páginas de 100; Fracttal devuelve 429
        # a mitad de camino si se pide sin pausa. Backoff exponencial.
        for intento in range(6):
            r = requests.get(f"{FRACTTAL_BASE}/api/items/", headers=h,
                             params={"limit": PAGE, "start": start}, timeout=60)
            if r.status_code != 429:
                break
            espera = 2 ** intento
            log(f"  429 en start={start}; reintentando en {espera}s")
            time.sleep(espera)
        r.raise_for_status()
        data = r.json().get("data") or []
        time.sleep(0.15)
        if not data:
            break
        for it in data:
            code = str(it.get("code") or "")
            if not code.upper().startswith("LOC-"):
                continue
            # "longitud" sin la 'e' final: así lo devuelve la API, no es un typo.
            lat, lon = it.get("latitude"), it.get("longitud")
            if lat in (None, "", 0) or lon in (None, "", 0):
                continue
            desc = it.get("description") or ""
            out.append({"code": code,
                        "desc": desc,
                        "lat": float(lat), "lon": float(lon),
                        "cliente": (it.get("field_1") or "").strip() or None,
                        "direccion": (it.get("field_2") or "").strip() or None,
                        "comuna": canonizar(it.get("field_3")) or comuna_de(desc),
                        "region": region_de(it.get("field_5"))})
        # La API ignora `limit` mayores a ~100: avanzar por lo realmente recibido,
        # no por el tamaño pedido, o se saltan registros silenciosamente.
        start += len(data)
    return out


def estaciones() -> list:
    rows, off = [], 0
    while True:
        r = requests.get(f"{SUPABASE_URL}/rest/v1/estaciones_servicio",
                         headers=_sb_headers(),
                         params={"select": "eds_occim,nombre,loc_fracttal,activa",
                                 "limit": 1000, "offset": off}, timeout=60)
        r.raise_for_status()
        b = r.json()
        if not b:
            break
        rows.extend(b)
        if len(b) < 1000:
            break
        off += 1000
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="Calcula y reporta, pero no escribe en Supabase.")
    args = ap.parse_args()

    token = get_token()
    locs = ubicaciones_fracttal(token)
    log(f"Ubicaciones Fracttal con coordenadas: {len(locs)}")

    por_code = {l["code"].upper(): l for l in locs}
    con_comuna = sum(1 for l in locs if l["comuna"])
    log(f"  con comuna: {con_comuna} "
        f"({con_comuna / max(1, len(locs)) * 100:.0f}%)")

    filas, sin_loc = [], []
    for e in estaciones():
        lf = str(e.get("loc_fracttal") or "").upper()
        loc = por_code.get(lf)
        if not loc:
            if e.get("activa"):
                sin_loc.append(e["eds_occim"])
            continue
        filas.append({
            "eds_occim": e["eds_occim"],
            "loc_fracttal": loc["code"],
            "latitud": loc["lat"],
            "longitud": loc["lon"],
            "comuna": loc["comuna"],
            "fuente": "loc_fracttal",
        })

    log(f"EDS con coordenadas: {len(filas)}")
    log(f"EDS activas SIN ubicación Fracttal: {len(sin_loc)}"
        + (f" -> {', '.join(sorted(sin_loc)[:12])}" if sin_loc else ""))

    if args.dry_run:
        log("--dry-run: no se escribe nada.")
        return

    escritas = 0
    for i in range(0, len(filas), 200):
        chunk = filas[i:i + 200]
        r = requests.post(f"{SUPABASE_URL}/rest/v1/{TABLE}",
                          headers={**_sb_headers(),
                                   "Content-Type": "application/json",
                                   "Prefer": "resolution=merge-duplicates"},
                          json=chunk, timeout=90)
        if r.status_code in (200, 201, 204):
            escritas += len(chunk)
        else:
            log(f"  ERROR {r.status_code}: {r.text[:200]}")
    log(f"✔ {TABLE}: {escritas} filas escritas")


if __name__ == "__main__":
    main()
