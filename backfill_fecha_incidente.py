"""
backfill_fecha_incidente.py
===========================
Llena `ordenes_trabajo.fecha_incidente` con la fecha REAL de la alerta del
cliente (`fecha_llamado`), que los 3 robots de correo capturan y persisten en
la tabla de llamados (expuesta vía la vista v_llamados_sla).

Contexto:
  - El inicio de la SLA = cuando el cliente envía el correo de alerta.
  - Post 2-jun-2026 los robots leen ese correo y guardan la hora en
    v_llamados_sla.fecha_llamado (≈ minutos antes de que Fracttal cree la OT).
  - `fecha_incidente` en ordenes_trabajo venía de event_date de Fracttal (vacío).
    Aquí la espejamos desde fecha_llamado para que la tabla de OTs sea coherente.

Regla:
  - Solo OTs con llamado (correctivas/emergencias) reciben fecha_incidente.
  - Preventivas / OTs sin llamado quedan en NULL (son programadas, no nacen de
    una alerta del cliente).

Uso:
  python backfill_fecha_incidente.py
  python backfill_fecha_incidente.py --desde 2026-06-02   (solo era robots)
"""

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

SUPABASE_URL   = "https://puefgkyjghwwgdfxbrex.supabase.co"
SUPABASE_KEY   = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6In"
    "B1ZWZna3lqZ2h3d2dkZnhicmV4Iiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc4"
    "MDcxMTk0OCwiZXhwIjoyMDk2Mjg3OTQ4fQ.keB15jRQ7ahuXiDHktFC_Yi000XUlExjDMqTuC6VLgw"
)
WORKERS = 16


def _h(write=False):
    h = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
         "Accept": "application/json"}
    if write:
        h["Content-Type"] = "application/json"
        h["Prefer"] = "return=minimal"
    return h


def fetch_llamados(desde: str) -> dict:
    """{os_fracttal: fecha_llamado} desde la vista, paginado."""
    out, offset, page = {}, 0, 1000
    while True:
        url = (f"{SUPABASE_URL}/rest/v1/v_llamados_sla"
               f"?select=os_fracttal,fecha_llamado"
               f"&fecha_llamado=gte.{desde}&fecha_llamado=not.is.null"
               f"&os_fracttal=not.is.null"
               f"&order=fecha_llamado.desc&limit={page}&offset={offset}")
        r = requests.get(url, headers=_h(), timeout=30)
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        for row in batch:
            os_f = row.get("os_fracttal")
            fl   = row.get("fecha_llamado")
            # Si una OS tiene varios llamados, conservar el más antiguo (1ra alerta)
            if os_f and fl:
                if os_f not in out or fl < out[os_f]:
                    out[os_f] = fl
        if len(batch) < page:
            break
        offset += page
    return out


def patch_incidente(folio: str, fecha: str) -> bool:
    payload = {"fecha_incidente": fecha}
    for intento in range(3):
        try:
            r = requests.patch(
                f"{SUPABASE_URL}/rest/v1/ordenes_trabajo?id_ot=eq.{folio}",
                headers=_h(write=True), data=json.dumps(payload), timeout=20,
            )
            return r.status_code in (200, 204)
        except requests.exceptions.RequestException:
            if intento == 2:
                return False
            time.sleep(2 * (intento + 1))
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--desde", default="2026-01-01")
    args = ap.parse_args()

    print(f"[1/2] Leyendo llamados (fecha_llamado) desde {args.desde}...")
    llamados = fetch_llamados(args.desde)
    print(f"      {len(llamados)} OTs con fecha de alerta del cliente.")

    if not llamados:
        print("      Nada que espejar.")
        return

    print(f"[2/2] Espejando fecha_llamado -> ordenes_trabajo.fecha_incidente...")
    ok = err = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(patch_incidente, f, fl): f for f, fl in llamados.items()}
        for i, fut in enumerate(as_completed(futs), 1):
            if fut.result():
                ok += 1
            else:
                err += 1
            if i % 200 == 0:
                print(f"      {i}/{len(llamados)} | ok={ok} err={err}")
    print(f"COMPLETADO: {ok} actualizadas, {err} errores.")


if __name__ == "__main__":
    main()
