"""
fix_aramco_priorities.py  v3
============================
Corrige prioridad de OTs Aramco/Esmax en Supabase.

ARQUITECTURA IMPORTANTE:
  En Supabase hay un trigger `fn_proteger_prioridad_robot` sobre
  ordenes_trabajo que en cada UPDATE sobreescribe prioridad_calc con
  el valor de llamados_correctivos.prioridad (si existe).

  ⇒ La FUENTE DE VERDAD es `llamados_correctivos.prioridad`.
  ⇒ Actualizar ordenes_trabajo.prioridad_calc directamente NO funciona
    (el trigger lo pisa). Este script actualiza llamados_correctivos.

Fuente Aramco: Cotalker Metabase.
  - N° Cotalker REAL = primer número al inicio de nota_tarea de la OT.
  - SLA -> prioridad: {24: P1, 48: P2, 72: P3, otros: P4}.
  - También corrige umbral_horas y n_aviso.

Uso:
    python fix_aramco_priorities.py            # dry-run (no actualiza)
    python fix_aramco_priorities.py --apply    # actualiza Supabase
"""

import re
import sys
import time
from datetime import datetime

import requests

SUPABASE_URL = "https://puefgkyjghwwgdfxbrex.supabase.co"
SUPABASE_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6In"
    "B1ZWZna3lqZ2h3d2dkZnhicmV4Iiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc4"
    "MDcxMTk0OCwiZXhwIjoyMDk2Mjg3OTQ4fQ.keB15jRQ7ahuXiDHktFC_Yi000XUlExjDMqTuC6VLgw"
)
METABASE_URL = (
    "https://bi.cotalker.com/api/public/card"
    "/56662edd-715d-4dbe-af9a-21891f4dbb97/query/json"
)

SLA_MAP = {24: "P1", 48: "P2", 72: "P3"}
_PAT_COT = re.compile(r"^(\d{5,8})(?:\s*-|\s*$)")

_H = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type":  "application/json",
    "Accept":        "application/json",
}


def fetch_metabase() -> dict:
    """Retorna {N° Cotalker(int): SLA esperado(int horas)}."""
    print("-> Descargando panel Metabase Cotalker...")
    r = requests.get(METABASE_URL, headers={"Accept": "application/json"}, timeout=60)
    r.raise_for_status()
    data = r.json()
    idx = {}
    for row in data:
        n   = row.get("N° Cotalker")
        sla = row.get("SLA esperado")
        if n and sla:
            try:
                idx[int(n)] = int(float(sla))
            except (ValueError, TypeError):
                pass
    print(f"  {len(idx)} OTs en Metabase con SLA")
    return idx


def sla_to_prio(sla: int) -> str:
    """Aramco: 24->P1, 48->P2, 72->P3, otros->P4."""
    return SLA_MAP.get(sla, "P4")


def fetch_aramco_ots() -> list:
    """OTs Aramco correctivas con nota_tarea desde ordenes_trabajo (para leer N° real)."""
    print("-> Descargando OTs Aramco desde ordenes_trabajo...")
    all_rows = []
    offset = 0
    while True:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/ordenes_trabajo"
            f"?cliente=eq.ESMAX (Aramco)"
            f"&tipo_tarea=eq.CORRECTIVA"
            f"&nota_tarea=not.is.null"
            f"&select=id_ot,nota_tarea,prioridad_calc"
            f"&order=fecha_creacion.desc"
            f"&limit=1000&offset={offset}",
            headers=_H, timeout=30,
        )
        rows = r.json()
        all_rows.extend(rows)
        if len(rows) < 1000:
            break
        offset += 1000
    print(f"  {len(all_rows)} OTs Aramco correctivas con nota_tarea")
    return all_rows


def fetch_llamados_map() -> dict:
    """
    Retorna {os_fracttal: {prioridad, n_aviso, umbral_horas}} de llamados_correctivos.
    NOTA: dos queries separadas por cliente porque PostgREST rompe con
    `cliente=in.(ESMAX,ESMAX (Aramco))` por los paréntesis internos.
    """
    print("-> Descargando llamados_correctivos Aramco...")
    all_rows = []
    for cliente in ("ESMAX", "ESMAX (Aramco)"):
        offset = 0
        while True:
            r = requests.get(
                f"{SUPABASE_URL}/rest/v1/llamados_correctivos"
                f"?cliente=eq.{cliente}"
                f"&select=os_fracttal,prioridad,n_aviso,umbral_horas"
                f"&limit=1000&offset={offset}",
                headers=_H, timeout=30,
            )
            rows = r.json()
            all_rows.extend(rows)
            if len(rows) < 1000:
                break
            offset += 1000
    idx = {r["os_fracttal"]: r for r in all_rows if r.get("os_fracttal")}
    print(f"  {len(idx)} entradas en llamados_correctivos")
    return idx


def patch_llamado(os_fracttal: str, prio: str, n_aviso: int, umbral: int) -> bool:
    """PATCH en llamados_correctivos (fuente de verdad para el trigger)."""
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/llamados_correctivos?os_fracttal=eq.{os_fracttal}",
        headers={**_H, "Prefer": "return=minimal"},
        json={
            "prioridad":    prio,
            "n_aviso":      str(n_aviso),
            "umbral_horas": umbral,
        },
        timeout=15,
    )
    return r.status_code in (200, 204)


def main():
    apply = "--apply" in sys.argv

    print("=" * 70)
    print(f"  FIX ARAMCO PRIORITIES v3 (via llamados_correctivos)")
    print(f"  Modo: {'APPLY' if apply else 'DRY-RUN'}")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)

    cotalker = fetch_metabase()
    ots      = fetch_aramco_ots()
    llamados = fetch_llamados_map()

    stats = {
        "total_ots":           len(ots),
        "sin_pattern":         0,
        "no_en_metabase":      0,
        "no_en_llamados":      0,
        "ya_correcta":         0,
        "actualizable":        0,
        "actualizada":         0,
        "errores":             0,
    }
    cambios = {}

    print()
    for r in ots:
        ot = r["id_ot"]
        m  = _PAT_COT.match(str(r.get("nota_tarea", "") or "").strip())
        if not m:
            stats["sin_pattern"] += 1
            continue
        n_real = int(m.group(1))
        sla_h  = cotalker.get(n_real)
        if sla_h is None:
            stats["no_en_metabase"] += 1
            continue
        prio_real = sla_to_prio(sla_h)

        # Buscar en llamados_correctivos (fuente de verdad del trigger)
        llam = llamados.get(ot)
        if llam is None:
            # No está en llamados_correctivos → el sync no dispara el trigger,
            # ordenes_trabajo.prioridad_calc queda con el valor del sync (correcto).
            # No hay nada que corregir aquí.
            stats["no_en_llamados"] += 1
            continue

        prio_actual   = llam.get("prioridad")
        n_aviso_actual= llam.get("n_aviso")
        umbral_actual = llam.get("umbral_horas")

        if (prio_actual == prio_real
            and str(n_aviso_actual) == str(n_real)
            and umbral_actual == sla_h):
            stats["ya_correcta"] += 1
            continue

        stats["actualizable"] += 1
        key = f"{prio_actual or 'None'}->{prio_real}"
        cambios[key] = cambios.get(key, 0) + 1

        if apply:
            if patch_llamado(ot, prio_real, n_real, sla_h):
                stats["actualizada"] += 1
                print(f"  [OK]  {ot}: prio {prio_actual}->{prio_real} | "
                      f"n_aviso {n_aviso_actual}->{n_real} | umbral {umbral_actual}->{sla_h}h")
            else:
                stats["errores"] += 1
                print(f"  [ERR] {ot}")
            time.sleep(0.03)
        else:
            print(f"  [DRY] {ot}: prio {prio_actual}->{prio_real} | "
                  f"n_aviso {n_aviso_actual}->{n_real} | umbral {umbral_actual}->{sla_h}h")

    print()
    print("=" * 70)
    print("  RESUMEN")
    print("=" * 70)
    for k, v in stats.items():
        print(f"  {k:20s}: {v}")
    print()
    print("  Cambios de prioridad:")
    for k, v in sorted(cambios.items(), key=lambda x: -x[1]):
        print(f"    {k:12s}: {v}")

    if not apply and stats["actualizable"] > 0:
        print()
        print(f"  >>> Ejecutar con --apply para actualizar {stats['actualizable']} OTs <<<")


if __name__ == "__main__":
    main()
