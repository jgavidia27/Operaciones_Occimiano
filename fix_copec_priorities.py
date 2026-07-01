"""
fix_copec_priorities.py  v2
============================
Corrige prioridad de OTs COPEC en llamados_correctivos.

ARQUITECTURA:
  El trigger fn_proteger_prioridad_robot en ordenes_trabajo sobreescribe
  prioridad_calc con llamados_correctivos.prioridad en cada UPDATE.
  ⇒ La fuente de verdad es llamados_correctivos.prioridad.

Fuente COPEC: nota_tarea contiene "Tiempo de respuesta: XX horas"
(guardado por el robot COPEC que lee correos del cliente).

Mapeo COPEC (horas -> prioridad):
  18h -> P1   |   24h -> P1   |   36h -> P3
  48h -> P2   |   72h -> P3   |   96h -> P4

Además de la prioridad, recalcula umbral_horas por zona:
  P1: RM=18 / Reg=24     P2: RM=24 / Reg=48
  P3: RM=36 / Reg=72     P4: cualquier zona=96

Uso:
    python fix_copec_priorities.py            # dry-run
    python fix_copec_priorities.py --apply    # aplica
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

_H = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type":  "application/json",
    "Accept":        "application/json",
}

_PAT_SLA = re.compile(r"Tiempo\s+de\s+respuesta\s*:\s*(\d+)", re.IGNORECASE)
_SLA_TO_PRIO = {18: "P1", 24: "P1", 36: "P3", 48: "P2", 72: "P3", 96: "P4"}
_UMBRALES = {
    "P1": (18, 24),   # (Santiago, Regiones)
    "P2": (24, 48),
    "P3": (36, 72),
    "P4": (96, 96),
}


def sla_from_nota(nota: str) -> "tuple[int, str] | None":
    """Retorna (sla_h, prioridad) o None."""
    if not nota:
        return None
    m = _PAT_SLA.search(nota)
    if not m:
        return None
    try:
        sla_h = int(m.group(1))
    except ValueError:
        return None
    prio = _SLA_TO_PRIO.get(sla_h)
    if not prio:
        return None
    return sla_h, prio


def umbral_por_zona(prio: str, zona_sla: str) -> int:
    """Devuelve horas SLA para (prioridad, zona)."""
    santiago = str(zona_sla or "").strip().lower() == "santiago"
    return _UMBRALES[prio][0 if santiago else 1]


def fetch_copec_ots() -> list:
    """OTs COPEC correctivas con nota_tarea."""
    print("-> Descargando OTs COPEC desde ordenes_trabajo...")
    all_rows = []
    offset = 0
    while True:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/ordenes_trabajo"
            f"?cliente=eq.COPEC"
            f"&tipo_tarea=eq.CORRECTIVA"
            f"&nota_tarea=not.is.null"
            f"&select=id_ot,codigo_eds,nota_tarea"
            f"&limit=1000&offset={offset}",
            headers=_H, timeout=30,
        )
        rows = r.json()
        all_rows.extend(rows)
        if len(rows) < 1000:
            break
        offset += 1000
    print(f"  {len(all_rows)} OTs COPEC correctivas con nota_tarea")
    return all_rows


def fetch_llamados_map() -> dict:
    """{os_fracttal: fila} de llamados_correctivos COPEC."""
    print("-> Descargando llamados_correctivos COPEC...")
    all_rows = []
    offset = 0
    while True:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/llamados_correctivos"
            f"?cliente=eq.COPEC"
            f"&select=os_fracttal,prioridad,umbral_horas"
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


def fetch_zonas_eds() -> dict:
    """{codigo_eds: zona_sla} desde estaciones_servicio."""
    print("-> Descargando zonas EDS...")
    all_rows = []
    offset = 0
    while True:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/estaciones_servicio"
            f"?select=eds_occim,zona_sla"
            f"&limit=1000&offset={offset}",
            headers=_H, timeout=30,
        )
        rows = r.json()
        all_rows.extend(rows)
        if len(rows) < 1000:
            break
        offset += 1000
    idx = {r["eds_occim"]: (r.get("zona_sla") or "Regiones") for r in all_rows if r.get("eds_occim")}
    print(f"  {len(idx)} EDS")
    return idx


def patch_llamado(os_fracttal: str, prio: str, umbral: int) -> bool:
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/llamados_correctivos?os_fracttal=eq.{os_fracttal}",
        headers={**_H, "Prefer": "return=minimal"},
        json={"prioridad": prio, "umbral_horas": umbral},
        timeout=15,
    )
    return r.status_code in (200, 204)


def touch_ot(id_ot: str, now_iso: str) -> bool:
    """Touch ordenes_trabajo para disparar el trigger fn_proteger_prioridad_robot."""
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/ordenes_trabajo?id_ot=eq.{id_ot}",
        headers={**_H, "Prefer": "return=minimal"},
        json={"updated_at": now_iso},
        timeout=15,
    )
    return r.status_code in (200, 204)


def main():
    apply = "--apply" in sys.argv

    print("=" * 70)
    print(f"  FIX COPEC PRIORITIES v2 (via llamados_correctivos)")
    print(f"  Modo: {'APPLY' if apply else 'DRY-RUN'}")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)

    ots      = fetch_copec_ots()
    llamados = fetch_llamados_map()
    zonas    = fetch_zonas_eds()

    stats = {
        "total_ots":      len(ots),
        "sin_pattern":    0,
        "no_en_llamados": 0,
        "ya_correcta":    0,
        "actualizable":   0,
        "actualizada":    0,
        "errores":        0,
    }
    cambios = {}

    from datetime import timezone
    now_iso = datetime.now(timezone.utc).isoformat()

    print()
    for r in ots:
        ot = r["id_ot"]
        res = sla_from_nota(r.get("nota_tarea", ""))
        if res is None:
            stats["sin_pattern"] += 1
            continue
        sla_h, prio_real = res
        eds  = r.get("codigo_eds")
        zona = zonas.get(eds, "Regiones")
        umbral_real = umbral_por_zona(prio_real, zona)

        llam = llamados.get(ot)
        if llam is None:
            stats["no_en_llamados"] += 1
            continue

        prio_actual   = llam.get("prioridad")
        umbral_actual = llam.get("umbral_horas")

        if prio_actual == prio_real and umbral_actual == umbral_real:
            stats["ya_correcta"] += 1
            continue

        stats["actualizable"] += 1
        key = f"{prio_actual or 'None'}->{prio_real}"
        cambios[key] = cambios.get(key, 0) + 1

        if apply:
            ok = patch_llamado(ot, prio_real, umbral_real)
            if ok:
                touch_ot(ot, now_iso)  # dispara el trigger para propagar a ordenes_trabajo
                stats["actualizada"] += 1
                print(f"  [OK]  {ot} [{zona}]: {prio_actual}->{prio_real} | umbral {umbral_actual}->{umbral_real}h")
            else:
                stats["errores"] += 1
                print(f"  [ERR] {ot}")
            time.sleep(0.03)
        else:
            print(f"  [DRY] {ot} [{zona}]: {prio_actual}->{prio_real} | umbral {umbral_actual}->{umbral_real}h")

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
