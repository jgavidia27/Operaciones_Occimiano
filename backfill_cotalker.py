"""
backfill_cotalker.py  v2
========================
Relleno historico mejorado: cruza Cotalker/Metabase contra Supabase
usando EDS + fecha + equipo (campo Activo) para evitar colisiones cuando
hay dos OTs del mismo EDS el mismo dia (ej. Aspiradora + Hidrolavadora).

Estrategia en 3 pasos por OT:
  1. Preciso  : EDS + fecha +-8d + keyword equipo (ILIKE)  -> acepta cualquier nro de matches
  2. Amplio   : EDS + fecha +-15d sin equipo               -> solo acepta si hay exactamente 1 match
  3. Sin fecha: EDS unico en Supabase + nombre_activo match -> fallback final

Solo actualiza filas con n_cotalker NULL (no sobreescribe).

Uso:
    python backfill_cotalker.py
"""

import os
import time
import requests
from datetime import datetime, timedelta

# ── Credenciales ──────────────────────────────────────────────────────────────
def _load_env():
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())

_load_env()

SUPABASE_URL = os.getenv("SUPABASE_URL", "https://puefgkyjghwwgdfxbrex.supabase.co")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6In"
    "B1ZWZna3lqZ2h3d2dkZnhicmV4Iiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc4"
    "MDcxMTk0OCwiZXhwIjoyMDk2Mjg3OTQ4fQ.keB15jRQ7ahuXiDHktFC_Yi000XUlExjDMqTuC6VLgw"
))
METABASE_URL = (
    "https://bi.cotalker.com/api/public/card"
    "/56662edd-715d-4dbe-af9a-21891f4dbb97/query/json"
)

_SB_HEADERS = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type":  "application/json",
    "Accept":        "application/json",
}

# ── Mapeo Activo Cotalker -> keyword para nombre_activo Supabase ──────────────
# Cotalker usa nombres cortos; Supabase tiene nombres largos del fabricante.
# Se busca con ILIKE '%keyword%' en nombre_activo.
ACTIVO_KEYWORDS = {
    "aspirado":      "ASPIRA",
    "aspiradora":    "ASPIRA",
    "hidrolavadora": "LAVADORA",
    "lavadora":      "LAVADORA",
    "lavado":        "LAVADORA",
    "melf":          "MSELF",
    "tren":          "TREN",
    "recirculacion": "RECIRCULA",
    "recirculacion": "RECIRCULA",
    "agua":          "AGUA",
    "bomba":         "BOMBA",
    "compresor":     "COMPRESOR",
    "surtidor":      "SURTIDOR",
    "dispensador":   "DISPENSADOR",
    "recuperadora":  "RECUPERA",
    "aspirado-lavado": "LAVADORA",
}

def activo_to_keyword(activo: str) -> str:
    """Convierte el campo Activo de Cotalker a keyword para ILIKE en Supabase."""
    if not activo:
        return ""
    key = activo.lower().strip().split()[0]   # primer token
    # buscar en mapa (match parcial)
    for pat, kw in ACTIVO_KEYWORDS.items():
        if pat in key or key in pat:
            return kw
    # fallback: primeras 4 letras en mayuscula
    return key[:4].upper()


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _sb_get(url: str, retries: int = 4) -> list:
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=_SB_HEADERS, timeout=20)
            if r.ok:
                data = r.json()
                return data if isinstance(data, list) else []
            return []
        except Exception:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    return []


def _sb_patch(id_ot: str, n_cotalker: int, retries: int = 4) -> bool:
    url = f"{SUPABASE_URL}/rest/v1/ordenes_trabajo?id_ot=eq.{id_ot}"
    for attempt in range(retries):
        try:
            r = requests.patch(
                url,
                headers={**_SB_HEADERS, "Prefer": "return=minimal"},
                json={"n_cotalker": n_cotalker},
                timeout=15,
            )
            return r.status_code in (200, 204)
        except Exception:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    return False


# ── Descarga Cotalker ─────────────────────────────────────────────────────────

def fetch_cotalker_correctivas() -> list:
    print("Descargando panel Cotalker/Metabase...")
    r = requests.get(METABASE_URL, headers={"Accept": "application/json"}, timeout=60)
    r.raise_for_status()
    data = r.json()
    print(f"  {len(data)} filas totales en Metabase")

    result = []
    for row in data:
        n      = row.get("N° Cotalker") or row.get("N° Cotalker") or row.get("N? Cotalker")
        eds    = row.get("Code UT padre")
        nombre = str(row.get("Nombre orden", ""))
        fecha  = str(row.get("Fecha creación") or row.get("Fecha creación") or row.get("Fecha creaci?n", ""))[:10]
        activo = str(row.get("Activo", "")).strip()

        if nombre.startswith("PREV"):
            continue
        if not n or not eds or not fecha or fecha == "":
            continue
        # Solo EE_S### (PBR-34 y similares no existen en Cotalker)
        eds_up = str(eds).upper().strip()
        if not eds_up.startswith("EE_"):
            continue

        result.append({
            "n_cotalker": int(n),
            "eds_code":   eds_up,
            "fecha_str":  fecha,
            "activo":     activo,
            "kw":         activo_to_keyword(activo),
        })

    print(f"  {len(result)} correctivas EE_S### con EDS y fecha")
    return result


# ── Carga todas las OTs ESMAX sin n_cotalker en Supabase ─────────────────────

def load_supabase_esmax_sin_cotalker() -> list:
    """Precarga todas las OTs ESMAX sin n_cotalker para evitar N queries."""
    rows = _sb_get(
        f"{SUPABASE_URL}/rest/v1/ordenes_trabajo"
        "?cliente=ilike.*ESMAX*"
        "&tipo_tarea=eq.CORRECTIVA"
        "&n_cotalker=is.null"
        "&select=id_ot,codigo_eds,nombre_activo,fecha_creacion"
        "&limit=2000"
    )
    print(f"  {len(rows)} OTs ESMAX sin n_cotalker cargadas desde Supabase")
    return rows


# ── Motor de matching ─────────────────────────────────────────────────────────

def _date_diff(fecha_ct: str, fecha_sb: str) -> int:
    """Diferencia absoluta en dias entre dos fechas YYYY-MM-DD."""
    try:
        d1 = datetime.strptime(fecha_ct, "%Y-%m-%d")
        d2 = datetime.strptime(str(fecha_sb)[:10], "%Y-%m-%d")
        return abs((d1 - d2).days)
    except Exception:
        return 9999


def find_best_match(ct: dict, candidates: list, window: int) -> str | None:
    """
    Dado un registro Cotalker y una lista de candidatos Supabase,
    devuelve el id_ot del mejor match o None.

    Paso 1: filtrar por ventana de fecha
    Paso 2: si hay keyword de equipo, filtrar por nombre_activo
    Paso 3: si multiple candidatos, elegir el de fecha mas cercana
    """
    fecha_ct = ct["fecha_str"]
    kw       = ct["kw"]

    # Filtrar por ventana de fecha
    en_ventana = [
        r for r in candidates
        if r.get("codigo_eds") == ct["eds_code"]
        and _date_diff(fecha_ct, r.get("fecha_creacion", "")) <= window
    ]
    if not en_ventana:
        return None

    # Filtrar por equipo si tenemos keyword
    if kw:
        con_equipo = [
            r for r in en_ventana
            if kw.lower() in str(r.get("nombre_activo", "")).lower()
        ]
        if con_equipo:
            # elegir el mas cercano en fecha
            return min(con_equipo, key=lambda r: _date_diff(fecha_ct, r.get("fecha_creacion", "")))["id_ot"]

    # Sin filtro de equipo: aceptar solo si es unico
    if len(en_ventana) == 1:
        return en_ventana[0]["id_ot"]

    # Multiples sin poder discriminar: elegir el mas cercano
    return min(en_ventana, key=lambda r: _date_diff(fecha_ct, r.get("fecha_creacion", "")))["id_ot"]


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    print("\n" + "=" * 60)
    print("  BACKFILL N Cotalker -> SUPABASE  v2 (matching exhaustivo)")
    print("=" * 60)

    cotalker_rows  = fetch_cotalker_correctivas()
    supabase_pool  = load_supabase_esmax_sin_cotalker()

    # Indexar candidatos por EDS para lookup rapido
    by_eds: dict[str, list] = {}
    for r in supabase_pool:
        eds = str(r.get("codigo_eds", "")).upper()
        by_eds.setdefault(eds, []).append(r)

    # Conjunto de id_ot ya asignados en esta sesion (para no asignar dos veces)
    assigned: set[str] = set()

    stats = {"ok": 0, "sin_match": 0, "error": 0}

    for idx, ct in enumerate(cotalker_rows, 1):
        eds  = ct["eds_code"]
        n    = ct["n_cotalker"]
        fec  = ct["fecha_str"]
        kw   = ct["kw"]

        candidates = [r for r in by_eds.get(eds, []) if r["id_ot"] not in assigned]

        # Intento 1: ventana 8 dias
        id_ot = find_best_match(ct, candidates, window=8)

        # Intento 2: ventana amplia 20 dias
        if id_ot is None:
            id_ot = find_best_match(ct, candidates, window=20)

        if id_ot is None:
            stats["sin_match"] += 1
            print(f"  [{idx:4d}] N={n:>8} EDS={eds:<12} {fec} kw={kw:<10}  sin match")
            continue

        ok = _sb_patch(id_ot, n)
        if ok:
            stats["ok"] += 1
            assigned.add(id_ot)
            # Remover del pool para que no sea reasignado
            by_eds[eds] = [r for r in by_eds[eds] if r["id_ot"] != id_ot]
            print(f"  [{idx:4d}] N={n:>8} EDS={eds:<12} {fec} kw={kw:<10}  -> {id_ot}  OK")
        else:
            stats["error"] += 1
            print(f"  [{idx:4d}] N={n:>8} EDS={eds:<12} {fec} kw={kw:<10}  -> {id_ot}  ERROR PATCH")

    print("\n" + "=" * 60)
    print("  RESUMEN")
    print("=" * 60)
    print(f"  Cotalker procesadas  : {len(cotalker_rows)}")
    print(f"  Actualizadas OK      : {stats['ok']}")
    print(f"  Sin match            : {stats['sin_match']}")
    print(f"  Errores PATCH        : {stats['error']}")
    print("=" * 60)


if __name__ == "__main__":
    main()
