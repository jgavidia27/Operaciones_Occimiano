"""
_audit_estim_neta.py
====================
Auditoría automática de duracion_estim_neta_seg en OTs preventivas 2026.

Detecta 3 clases de errores que causarían mostrar tiempos sumados en el
dashboard de Precisión Fracttal:

  1. FALTAN — Preventiva 2026 sin duracion_estim_neta_seg poblado.
     → sync_estim_neta.py aún no la procesó.

  2. INFLADO — La neta === total (o casi), sugiere que el sync marcó
     "sin lavadora" cuando en Fracttal SÍ hay lavadora, o que sumó
     subtareas auxiliares (INSPECCIÓN, CAMBIO ACEITE).

  3. RATIO ABSURDO — Real neta muy grande frente a estimada
     (>500 %). Suele indicar que real se dejó como total original
     mientras estim se ajustó, o viceversa.

Uso:
    python _audit_estim_neta.py                  (última semana)
    python _audit_estim_neta.py --desde 2026-06-01
    python _audit_estim_neta.py --fix            (corre el sync en OTs sin neta)
"""

import argparse
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta

import requests

SUPABASE_URL = "https://puefgkyjghwwgdfxbrex.supabase.co"
SUPABASE_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6In"
    "B1ZWZna3lqZ2h3d2dkZnhicmV4Iiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc4"
    "MDcxMTk0OCwiZXhwIjoyMDk2Mjg3OTQ4fQ.keB15jRQ7ahuXiDHktFC_Yi000XUlExjDMqTuC6VLgw"
)
H = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}

_BASURA = {
    "ERROR DE INGRESO", "DUPLICADO", "Duplicidad",
    "DE PRUEBA", "PRUEBA ROBOT",
    "Cancelado", "Canceladas", "Cancelada",
}


def fetch_prev(desde: str) -> list:
    """OTs preventivas 2026 con datos de tiempos."""
    rows = []
    off = 0
    while True:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/ordenes_trabajo",
            headers=H,
            params={
                "select": "id_ot,fecha_creacion,estado,duracion_estim_seg,"
                          "duracion_estim_neta_seg,duracion_real_seg,"
                          "duracion_real_neta_seg",
                "tipo_tarea": "ilike.*PREVENTIVA*",
                "fecha_creacion": f"gte.{desde}",
                "order": "id_ot.desc",
                "limit": 1000,
                "offset": off,
            },
            timeout=30,
        )
        batch = r.json()
        if not batch or not isinstance(batch, list):
            break
        rows.extend(batch)
        if len(batch) < 1000:
            break
        off += 1000
    return rows


def audit(rows: list) -> dict:
    """Clasifica las OTs en las clases de problema + OK.

    Nota: NO clasifica como 'inflado' cuando neta == total y la OT es
    de una sola subtarea — es el caso legítimo de OTs sin lavadora
    donde el KPI evalúa el total del activo principal (aspiradora,
    etc.) o de solo-lavadora (neta = total = 1 subtarea).
    """
    faltan = []
    ratio_absurdo = []
    ok = []
    for r in rows:
        if r.get("estado") in _BASURA:
            continue
        et = r.get("duracion_estim_seg") or 0
        en = r.get("duracion_estim_neta_seg")
        rt = r.get("duracion_real_seg") or 0
        rn = r.get("duracion_real_neta_seg")
        if en is None:
            faltan.append(r)
            continue
        # ratio absurdo: real >> estim (data noise, no bug de suma)
        if en > 0 and rn and rn > en * 5:
            ratio_absurdo.append(r)
        if en is not None and en < et * 0.9:
            ok.append(r)
    return {"faltan": faltan, "ratio": ratio_absurdo, "ok": ok}


def run_sync(folios: list) -> None:
    """Ejecuta sync_estim_neta para los folios indicados en batches de 100."""
    for i in range(0, len(folios), 100):
        batch = folios[i:i+100]
        cmd = [sys.executable, "sync_estim_neta.py", "--folios", ",".join(batch)]
        print(f"[fix] Corriendo sync para {len(batch)} folios ({i+1}-{i+len(batch)})...")
        subprocess.run(cmd, check=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--desde", default=(datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d"),
                    help="Fecha desde (YYYY-MM-DD). Default: hace 7 días.")
    ap.add_argument("--fix", action="store_true",
                    help="Ejecutar sync_estim_neta.py sobre las OTs sin neta.")
    args = ap.parse_args()

    print(f"[audit] Cargando preventivas desde {args.desde}...")
    rows = fetch_prev(args.desde)
    print(f"[audit] {len(rows)} preventivas encontradas.")

    r = audit(rows)
    total = len(rows)
    print()
    print("─" * 60)
    print(f"  Resumen de auditoría")
    print("─" * 60)
    print(f"  {'FALTA neta (no evaluable):':<40} {len(r['faltan']):>5}")
    print(f"  {'RATIO ABSURDO (real > 5x estim, data noise):':<40} {len(r['ratio']):>5}")
    print(f"  {'OK (neta < total, correcto):':<40} {len(r['ok']):>5}")
    print("─" * 60)

    if r["faltan"]:
        print("\n  FALTAN — muestra (primeras 10):")
        for x in r["faltan"][:10]:
            print(f"    {x['id_ot']:<12} {x['fecha_creacion'][:10]} · estado={x.get('estado','?')}")

    if r["ratio"]:
        print("\n  RATIO ABSURDO — no es bug de suma; data noise de real_duration.")
        print("  Muestra (primeras 5):")
        for x in r["ratio"][:5]:
            en = (x.get("duracion_estim_neta_seg") or 0) // 60
            rn = (x.get("duracion_real_neta_seg") or 0) // 60
            print(f"    {x['id_ot']:<12} est={en}min  real={rn}min  ({rn/en*100:.0f}%)")

    if args.fix and r["faltan"]:
        print()
        print(f"[fix] Corriendo sync para {len(r['faltan'])} OTs sin neta...")
        run_sync([x["id_ot"] for x in r["faltan"]])
        print("[fix] COMPLETADO. Re-corre este audit para verificar.")

    # Exit code para monitoreo: 0 si todo OK, 1 si hay problemas críticos (faltan)
    sys.exit(0 if len(r["faltan"]) == 0 else 1)


if __name__ == "__main__":
    main()
