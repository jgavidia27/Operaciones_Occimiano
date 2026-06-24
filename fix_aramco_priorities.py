"""
fix_aramco_priorities.py
========================
Corrige prioridad_calc y n_cotalker en Supabase para OTs de ESMAX/Aramco.

Problema detectado:
  - robot_esmax.py asignó N° Cotalker a OTs incorrectas (matching laxo por
    EDS + fecha sin validar). ~22% de las OTs Aramco tienen n_cotalker desplazado.
  - Eso arrastró también prioridad_calc incorrecta (P1/P2/P3 mal asignadas).

Fuente de verdad:
  - El N° Cotalker REAL de cada OT está al inicio de nota_tarea:
    "151022 - 169357 - ee_s268 - EDS: ANTOFAGASTA/COANIQUEM - ..."
                                      ↑ primer número = N° Cotalker real
  - Con ese N° consultamos Metabase para obtener el SLA esperado real.
  - SLA -> prioridad: {24: P1, 48: P2, 72: P3, otros: P4}

Uso:
    python fix_aramco_priorities.py            # dry-run (no actualiza)
    python fix_aramco_priorities.py --apply    # actualiza Supabase
"""

import os, re, sys, json, time, requests
from datetime import datetime

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
_PAT_COT = re.compile(r"^(\d{5,8})\s*-")

_H = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type":  "application/json",
    "Accept":        "application/json",
}


def fetch_metabase():
    print("-> Descargando panel Metabase Cotalker...")
    r = requests.get(METABASE_URL, headers={"Accept": "application/json"}, timeout=60)
    r.raise_for_status()
    data = r.json()
    idx = {int(row["N° Cotalker"]): row for row in data if row.get("N° Cotalker")}
    print(f"  {len(idx)} OTs en Metabase ({sum(1 for r in data if r.get('SLA esperado'))} con SLA)")
    return idx


def sla_to_prio(sla):
    if sla is None: return None
    try: h = int(float(sla))
    except: return None
    return SLA_MAP.get(h, "P4")


def fetch_aramco_ots():
    print("-> Descargando OTs Aramco desde Supabase...")
    all_rows = []
    offset = 0
    while True:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/ordenes_trabajo"
            f"?cliente=eq.ESMAX (Aramco)"
            f"&tipo_tarea=eq.CORRECTIVA"
            f"&nota_tarea=not.is.null"
            f"&select=id_ot,n_cotalker,prioridad_calc,nota_tarea,fecha_creacion"
            f"&order=fecha_creacion.desc"
            f"&limit=1000&offset={offset}",
            headers=_H, timeout=30,
        )
        rows = r.json()
        all_rows.extend(rows)
        if len(rows) < 1000: break
        offset += 1000
    print(f"  {len(all_rows)} OTs Aramco correctivas con nota_tarea")
    return all_rows


def patch(id_ot, n_cot, prio):
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/ordenes_trabajo?id_ot=eq.{id_ot}",
        headers={**_H, "Prefer": "return=minimal"},
        json={"n_cotalker": int(n_cot), "prioridad_calc": prio},
        timeout=15,
    )
    return r.status_code in (200, 204)


def main():
    apply = "--apply" in sys.argv

    print("="*70)
    print(f"  FIX ARAMCO PRIORITIES  —  {'APPLY' if apply else 'DRY-RUN'}")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*70)

    cotalker = fetch_metabase()
    rows = fetch_aramco_ots()

    stats = {
        "total":           len(rows),
        "sin_pattern":     0,
        "no_en_metabase":  0,
        "sin_sla":         0,
        "ya_correcta":     0,
        "actualizable":    0,
        "actualizada":     0,
        "errores":         0,
    }
    cambios_prio_calc = {"P1->P1": 0, "P1->P2": 0, "P1->P3": 0, "P1->P4": 0,
                         "P2->P1": 0, "P2->P2": 0, "P2->P3": 0, "P2->P4": 0,
                         "P3->P1": 0, "P3->P2": 0, "P3->P3": 0, "P3->P4": 0,
                         "P4->P1": 0, "P4->P2": 0, "P4->P3": 0, "P4->P4": 0,
                         "None->*":  0}
    print()
    for r in rows:
        ot = r["id_ot"]
        m = _PAT_COT.match(str(r.get("nota_tarea","") or "").strip())
        if not m:
            stats["sin_pattern"] += 1
            continue
        n_real = int(m.group(1))
        info = cotalker.get(n_real)
        if not info:
            stats["no_en_metabase"] += 1
            continue
        sla = info.get("SLA esperado")
        prio_real = sla_to_prio(sla)
        if prio_real is None:
            stats["sin_sla"] += 1
            continue
        prio_actual = r.get("prioridad_calc")
        n_actual    = r.get("n_cotalker")

        if n_actual == n_real and prio_actual == prio_real:
            stats["ya_correcta"] += 1
            continue

        # Necesita actualización
        stats["actualizable"] += 1
        key = f"{prio_actual or 'None'}->{prio_real}"
        if prio_actual is None:
            cambios_prio_calc["None->*"] += 1
        else:
            cambios_prio_calc[key] = cambios_prio_calc.get(key, 0) + 1

        if apply:
            if patch(ot, n_real, prio_real):
                stats["actualizada"] += 1
                print(f"  [OK] {ot}: cotalker {n_actual}->{n_real}, prio {prio_actual}->{prio_real} (SLA={sla}h)")
            else:
                stats["errores"] += 1
                print(f"  [ERR] {ot}")
            time.sleep(0.05)
        else:
            print(f"  [DRY] {ot}: cotalker {n_actual}->{n_real}, prio {prio_actual}->{prio_real} (SLA={sla}h)")

    print()
    print("="*70)
    print("  RESUMEN")
    print("="*70)
    for k, v in stats.items():
        print(f"  {k:20s}: {v}")
    print()
    print("  Cambios de prioridad:")
    for k, v in sorted(cambios_prio_calc.items()):
        if v > 0:
            print(f"    {k:10s}: {v}")
    if not apply and stats["actualizable"] > 0:
        print()
        print(f"  >>> Ejecutar con --apply para actualizar {stats['actualizable']} OTs <<<")


if __name__ == "__main__":
    main()
