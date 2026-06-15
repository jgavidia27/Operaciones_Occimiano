"""
backfill_cotalker.py
====================
Relleno histórico: cruza TODAS las OTs de Cotalker/Metabase contra
Supabase y guarda n_cotalker en la tabla ordenes_trabajo.

Uso (correr una sola vez después de crear la columna en Supabase):
    python backfill_cotalker.py

Requiere la columna n_cotalker en Supabase:
    ALTER TABLE ordenes_trabajo ADD COLUMN IF NOT EXISTS n_cotalker INTEGER;
"""

import os
import re
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

WINDOW_DAYS = 5   # ventana de ±días para cruzar fechas

_SB_HEADERS = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type":  "application/json",
    "Accept":        "application/json",
}

# ── 1. Descargar Cotalker/Metabase ────────────────────────────────────────────

def fetch_cotalker_correctivas():
    """
    Descarga todas las OTs correctivas de Cotalker con EDS y fecha.
    Retorna lista de dicts con: n_cotalker, eds_code, fecha_str.
    """
    print("Descargando panel Cotalker/Metabase...")
    r = requests.get(METABASE_URL, headers={"Accept": "application/json"}, timeout=60)
    r.raise_for_status()
    data = r.json()
    print(f"  {len(data)} filas totales en Metabase")

    result = []
    for row in data:
        n = row.get("N° Cotalker")
        eds = row.get("Code UT padre")
        nombre = str(row.get("Nombre orden", ""))
        fecha = str(row.get("Fecha creación", ""))[:10]

        # Omitir preventivas y filas sin datos clave
        if nombre.startswith("PREV"):
            continue
        if not n or not eds or not fecha:
            continue

        result.append({
            "n_cotalker": int(n),
            "eds_code":   str(eds).lower().strip(),
            "fecha_str":  fecha,
        })

    print(f"  {len(result)} correctivas con EDS y fecha")
    return result


# ── 2. Buscar OT en Supabase ──────────────────────────────────────────────────

def find_supabase_ot(eds_code: str, fecha_str: str) -> str | None:
    """
    Busca id_ot en Supabase por codigo_eds + fecha_creacion ±WINDOW_DAYS.
    Retorna el id_ot (e.g. 'OS-37735') o None.
    """
    try:
        fecha = datetime.strptime(fecha_str, "%Y-%m-%d")
    except ValueError:
        return None

    f_min = (fecha - timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%dT00:00:00")
    f_max = (fecha + timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%dT23:59:59")

    params = (
        f"select=id_ot,n_cotalker"
        f"&codigo_eds=eq.{eds_code}"
        f"&tipo_tarea=eq.CORRECTIVA"
        f"&cliente=ilike.*ESMAX*"
        f"&fecha_creacion=gte.{f_min}"
        f"&fecha_creacion=lte.{f_max}"
        f"&order=fecha_creacion.asc"
        f"&limit=3"
    )
    url = f"{SUPABASE_URL}/rest/v1/ordenes_trabajo?{params}"
    resp = requests.get(url, headers=_SB_HEADERS, timeout=15)
    if not resp.ok:
        return None
    rows = resp.json()
    if not isinstance(rows, list) or not rows:
        return None
    # Preferir la que aún no tenga n_cotalker
    sin_cotalker = [r for r in rows if not r.get("n_cotalker")]
    return (sin_cotalker[0] if sin_cotalker else rows[0])["id_ot"]


# ── 3. Actualizar n_cotalker en Supabase ──────────────────────────────────────

def set_n_cotalker(id_ot: str, n_cotalker: int) -> bool:
    url = f"{SUPABASE_URL}/rest/v1/ordenes_trabajo?id_ot=eq.{id_ot}"
    r = requests.patch(
        url,
        headers={**_SB_HEADERS, "Prefer": "return=minimal"},
        json={"n_cotalker": n_cotalker},
        timeout=10,
    )
    return r.status_code in (200, 204)


# ── 4. MAIN ───────────────────────────────────────────────────────────────────

def main():
    print("\n" + "=" * 60)
    print("  BACKFILL N° COTALKER → SUPABASE")
    print("=" * 60)

    cotalker_rows = fetch_cotalker_correctivas()

    stats = {"match": 0, "actualizado": 0, "ya_tenia": 0, "sin_match": 0, "error": 0}

    for idx, ct in enumerate(cotalker_rows, 1):
        n   = ct["n_cotalker"]
        eds = ct["eds_code"]
        fec = ct["fecha_str"]

        id_ot = find_supabase_ot(eds, fec)
        if id_ot is None:
            stats["sin_match"] += 1
            if idx <= 20 or idx % 50 == 0:
                print(f"  [{idx:4d}] N°{n:>8} EDS={eds:<12} {fec}  → sin match")
            continue

        stats["match"] += 1
        ok = set_n_cotalker(id_ot, n)
        if ok:
            stats["actualizado"] += 1
            print(f"  [{idx:4d}] N°{n:>8} EDS={eds:<12} {fec}  → {id_ot}  ✓")
        else:
            stats["error"] += 1
            print(f"  [{idx:4d}] N°{n:>8} EDS={eds:<12} {fec}  → {id_ot}  ERROR PATCH")

    print("\n" + "=" * 60)
    print("  RESUMEN")
    print("=" * 60)
    print(f"  Cotalker procesadas  : {len(cotalker_rows)}")
    print(f"  Con match Supabase   : {stats['match']}")
    print(f"  Actualizadas OK      : {stats['actualizado']}")
    print(f"  Sin match            : {stats['sin_match']}")
    print(f"  Errores PATCH        : {stats['error']}")
    print("=" * 60)


if __name__ == "__main__":
    main()
